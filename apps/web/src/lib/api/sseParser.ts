/** Incremental SSE framing. Line endings and UTF-8 packet boundaries are independent. */
export class SSEParser {
  private pending = '';
  private event = 'message';
  private data: string[] = [];
  private size = 0;
  private skipLF = false;

  constructor(
    private dispatch: (event: string, value: unknown) => void,
    private maxSize = 1_048_576,
  ) {}

  feed(chunk: string) {
    if (!chunk) return;
    let start = 0;
    if (this.skipLF) {
      if (chunk[0] === '\n') start = 1;
      this.skipLF = false;
    }
    for (let index = start; index < chunk.length; index++) {
      const character = chunk[index];
      if (character !== '\n' && character !== '\r') continue;
      this.line(this.pending + chunk.slice(start, index));
      this.pending = '';
      if (character === '\r') {
        if (chunk[index + 1] === '\n') index++;
        else if (index === chunk.length - 1) this.skipLF = true;
      }
      start = index + 1;
    }
    this.pending += chunk.slice(start);
    if (this.pending.length + this.size > this.maxSize)
      throw new Error('流式事件超过大小限制');
  }

  private line(line: string) {
    if (line === '') {
      if (this.data.length) {
        const raw = this.data.join('\n');
        let value: unknown = raw;
        try {
          value = JSON.parse(raw);
        } catch {
          /* SSE also supports text payloads. */
        }
        this.dispatch(this.event, value);
      }
      this.event = 'message';
      this.data = [];
      this.size = 0;
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value || 'message';
    if (field === 'data') {
      this.size += value.length + 1;
      if (this.size > this.maxSize) throw new Error('流式事件超过大小限制');
      this.data.push(value);
    }
  }
}
