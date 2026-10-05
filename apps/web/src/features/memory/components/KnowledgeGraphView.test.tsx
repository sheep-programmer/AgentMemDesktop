import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { mockGraphData } from '@/lib/api/mock/data';
import { KnowledgeGraphView } from './KnowledgeGraphView';

vi.mock('react-force-graph-2d', () => ({ default: ({ width, height }: { width: number; height: number }) => <output data-testid="canvas-size">{width}×{height}</output> }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('sizes the graph to its workspace rather than the full browser width', () => {
  let measure!: () => void;
  const disconnect = vi.fn();
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { measure = callback; }
    observe() {}
    disconnect = disconnect;
  });
  const { unmount } = render(<KnowledgeGraphView data={mockGraphData} />);
  const container = screen.getByRole('region', { name: '知识图谱' });
  Object.defineProperty(container, 'clientWidth', { value: 700, configurable: true });
  Object.defineProperty(container, 'clientHeight', { value: 650, configurable: true });
  act(() => measure());
  expect(screen.getByTestId('canvas-size').textContent).toBe('700×650');
  Object.defineProperty(container, 'clientWidth', { value: 280, configurable: true });
  act(() => measure());
  expect(screen.getByTestId('canvas-size').textContent).toBe('280×650');
  unmount();
  expect(disconnect).toHaveBeenCalled();
});
