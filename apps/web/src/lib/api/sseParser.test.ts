import { describe, expect, it, vi } from 'vitest';
import { SSEParser } from './sseParser';

describe('SSE network framing', () => {
  it('supports mixed line endings even when CRLF is split across packets', () => {
    const receive = vi.fn();
    const parser = new SSEParser(receive);
    parser.feed('event: delta\r');
    parser.feed('\ndata: {"text":"你好"}\r');
    parser.feed('\n\r');
    parser.feed('\nevent: done\ndata: {}\n\n');
    expect(receive.mock.calls).toEqual([
      ['delta', { text: '你好' }],
      ['done', {}],
    ]);
  });

  it('preserves whitespace and multiple data lines, ignoring heartbeat comments', () => {
    const receive = vi.fn();
    const parser = new SSEParser(receive);
    parser.feed(
      ': ping\r\rdata:  indented\ndata: second  \n\nevent: x\ndata:\n\n',
    );
    expect(receive.mock.calls).toEqual([
      ['message', ' indented\nsecond  '],
      ['x', ''],
    ]);
  });

  it('does not publish truncated frames at EOF and resets event names', () => {
    const receive = vi.fn();
    const parser = new SSEParser(receive);
    parser.feed('event: first\ndata: 1\n\ndata: 2\n\ndata: {"truncated":');
    expect(receive.mock.calls).toEqual([
      ['first', 1],
      ['message', 2],
    ]);
  });

  it('bounds memory for streams without a delimiter', () => {
    const parser = new SSEParser(() => {}, 20);
    expect(() => parser.feed('data: ' + 'x'.repeat(30))).toThrow('大小限制');
    const multiline = new SSEParser(() => {}, 20);
    expect(() =>
      multiline.feed(
        'data: ' + 'x'.repeat(12) + '\ndata: ' + 'x'.repeat(12) + '\n',
      ),
    ).toThrow('大小限制');
  });
});
