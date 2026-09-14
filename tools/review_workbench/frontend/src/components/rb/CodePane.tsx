import { useState, useRef, useEffect, useMemo } from 'react';
import type { ReviewEntry, LeftPaneState, RightPaneState } from '../../data/reviewData';
import { tokenizeLine } from './tokenizer';

interface CodePaneProps {
  side: 'ida' | 'cpp';
  entry: ReviewEntry;
  leftState: LeftPaneState;
  rightState: RightPaneState;
  onOpenInIda: () => void;
  onRefreshIda: () => void;
  onOpenInVsCode: () => void;
  onReload: () => void;
  onSelectCandidate: (path: string) => void;
  scrollRef?: React.RefObject<HTMLDivElement | null>;
  syncScroll: boolean;
  onScroll?: (top: number) => void;
  externalScrollTop?: number;
}

function CodeBlock({ code, startLine = 1 }: { code: string; startLine?: number }) {
  const lines = useMemo(() => code.split('\n'), [code]);
  const tokenLines = useMemo(() => lines.map(tokenizeLine), [lines]);
  return (
    <div className="overflow-auto flex-1 py-3">
      <table className="w-full border-collapse">
        <tbody>
          {lines.map((line, i) => {
            const tokens = tokenLines[i];
            return (
              <tr key={i} className="code-line group">
                <td className="select-none text-right pr-4 pl-3 text-tn-dim2 font-mono text-xs leading-5 w-10 align-top sticky left-0 bg-tn-bg group-hover:bg-tn-surface/30">
                  {startLine + i}
                </td>
                <td className="pr-6 font-mono text-xs leading-5 whitespace-pre align-top">
                  {tokens.map((tok, j) => (
                    <span key={j} className={tok.cls}>{tok.text}</span>
                  ))}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PaneHeader({
  side, entry, leftState, rightState,
  onOpenInIda, onRefreshIda, onOpenInVsCode, onReload,
}: Pick<CodePaneProps, 'side' | 'entry' | 'leftState' | 'rightState' | 'onOpenInIda' | 'onRefreshIda' | 'onOpenInVsCode' | 'onReload'>) {
  if (side === 'ida') {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 bg-tn-surf2 border-b border-tn-border flex-shrink-0">
        <span className="text-tn-dim text-[12px] uppercase tracking-wide font-semibold">IDA Pseudocode</span>
        <div className="w-px h-3 bg-tn-border" />
        <span className="text-tn-dim2 font-mono text-[12px]">{entry.srcModule || 'unbound'}</span>
        <span className="text-tn-orange font-mono text-[12px]">{entry.srcAddress || '--'}</span>
        <div className="flex-1" />
        {leftState === 'stale' && (
          <span className="text-tn-yellow text-[12px] flex items-center gap-1">
            <span>⚠</span> Stale snapshot, refresh failed
          </span>
        )}
        {leftState === 'loading' && (
          <span className="text-tn-blue text-[12px] animate-pulse">Loading…</span>
        )}
        <button
          onClick={onOpenInIda}
          className="text-[12px] px-2 py-0.5 border border-tn-border rounded text-tn-blue hover:bg-tn-surface"
        >
          Open in IDA
        </button>
        <button
          onClick={onRefreshIda}
          className="text-[12px] px-2 py-0.5 border border-tn-border rounded text-tn-dim hover:bg-tn-surface"
        >
          ↻ Refresh
        </button>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 bg-tn-surf2 border-b border-tn-border flex-shrink-0">
      <span className="text-tn-dim text-[12px] uppercase tracking-wide font-semibold">C++ Source</span>
      <div className="w-px h-3 bg-tn-border" />
      <span className="text-tn-cyan font-mono text-[12px] truncate max-w-[200px]" title={entry.srcFile}>
        {entry.srcFile}
      </span>
      <span className="text-tn-dim2 text-[12px]">:{entry.srcLine}</span>
      <div className="flex-1" />
      {rightState === 'loading' && (
        <span className="text-tn-blue text-[12px] animate-pulse">Indexing…</span>
      )}
      <button
        onClick={onOpenInVsCode}
        className="text-[12px] px-2 py-0.5 border border-tn-border rounded text-tn-purple hover:bg-tn-surface"
      >
        Open in VS Code
      </button>
      <button
        onClick={onReload}
        className="text-[12px] px-2 py-0.5 border border-tn-border rounded text-tn-dim hover:bg-tn-surface"
      >
        ↻ Reload
      </button>
    </div>
  );
}

function MappingBadge({ entry }: { entry: ReviewEntry }) {
  return (
    <div className="flex items-center gap-1.5 px-3 py-1 bg-tn-bg2 border-b border-tn-border text-[12px] flex-shrink-0">
      <span className="text-tn-dim">Mapping:</span>
      <span className="font-mono text-tn-orange">{entry.module}</span>
      <span className="font-mono text-tn-orange">{entry.address}</span>
      <span className="text-tn-dim2">→</span>
      <span className="font-mono text-tn-cyan">{entry.srcModule}</span>
      <span className="font-mono text-tn-cyan">{entry.srcAddress}</span>
      <span className="text-tn-dim2 ml-1">Target:{entry.targetId}</span>
    </div>
  );
}

export default function CodePane({
  side, entry, leftState, rightState,
  onOpenInIda, onRefreshIda, onOpenInVsCode, onReload, onSelectCandidate,
  scrollRef, syncScroll, onScroll, externalScrollTop,
}: CodePaneProps) {
  const [search, setSearch] = useState('');
  const bodyRef = useRef<HTMLDivElement>(null);
  const ignoreScroll = useRef(false);

  // Expose scrollRef
  useEffect(() => {
    if (scrollRef && bodyRef.current) {
      (scrollRef as React.MutableRefObject<HTMLDivElement | null>).current = bodyRef.current;
    }
  }, [scrollRef]);

  // Receive external scroll
  useEffect(() => {
    if (!syncScroll || externalScrollTop === undefined || !bodyRef.current) return;
    ignoreScroll.current = true;
    bodyRef.current.scrollTop = externalScrollTop;
    setTimeout(() => { ignoreScroll.current = false; }, 50);
  }, [externalScrollTop, syncScroll]);

  function handleScroll() {
    if (!syncScroll || ignoreScroll.current || !bodyRef.current || !onScroll) return;
    onScroll(bodyRef.current.scrollTop);
  }

  const code = side === 'ida' ? entry.pseudocode : entry.sourceCode;
  const startLine = side === 'cpp' ? entry.srcLine : 1;

  // Filter code lines for search
  const lines = useMemo(() => code.split('\n'), [code]);
  const tokenLines = useMemo(() => lines.map(tokenizeLine), [lines]);
  const matchIndices = search.trim()
    ? lines.reduce<number[]>((acc, l, i) => { if (l.toLowerCase().includes(search.toLowerCase())) acc.push(i); return acc; }, [])
    : [];

  return (
    <div className="flex flex-col flex-1 min-w-0 bg-tn-bg overflow-hidden">
      <PaneHeader
        side={side} entry={entry} leftState={leftState} rightState={rightState}
        onOpenInIda={onOpenInIda} onRefreshIda={onRefreshIda}
        onOpenInVsCode={onOpenInVsCode} onReload={onReload}
      />
      <MappingBadge entry={entry} />

      {/* search bar */}
      <div className="flex items-center gap-2 px-3 py-1 bg-tn-bg2 border-b border-tn-border flex-shrink-0">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={`Search ${side === 'ida' ? 'pseudocode' : 'source'}…`}
          className="bg-tn-surf2 border border-tn-border text-tn-text text-xs rounded px-2 py-0.5 w-48 focus:outline-none focus:border-tn-blue placeholder-tn-dim font-mono"
        />
        {search && (
          <span className="text-tn-dim text-[12px]">
            {matchIndices.length} match{matchIndices.length !== 1 ? 'es' : ''}
          </span>
        )}
        {search && (
          <button onClick={() => setSearch('')} className="text-tn-dim text-xs hover:text-tn-text">✕</button>
        )}
      </div>

      {/* content */}
      {side === 'ida' && leftState === 'failed' && (
        <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center px-6">
          <span className="text-tn-red text-2xl">⚠</span>
          <div className="text-tn-text text-sm font-medium">Decompilation Failed</div>
          <div className="text-tn-dim text-xs max-w-xs">
            No current Pseudocode snapshot. Check the connection, module mapping and the message above.
          </div>
          <button
            onClick={onOpenInIda}
            className="px-3 py-1.5 bg-tn-surface border border-tn-border rounded text-xs text-tn-blue hover:bg-tn-surf2"
          >
            Open in IDA to repair
          </button>
        </div>
      )}

      {side === 'cpp' && rightState === 'not-found' && (
        <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center px-6">
          <span className="text-tn-yellow text-2xl">?</span>
          <div className="text-tn-text text-sm font-medium">Source Not Found</div>
          <div className="text-tn-dim text-xs max-w-xs">
            No C++ function matched the address comment
            <span className="font-mono text-tn-cyan ml-1">{entry.address}:{entry.module}</span>.
            The source file may not be indexed yet.
          </div>
          <button
            onClick={onReload}
            className="px-3 py-1.5 bg-tn-surface border border-tn-border rounded text-xs text-tn-yellow hover:bg-tn-surf2"
          >
            Retry indexing
          </button>
        </div>
      )}

      {side === 'cpp' && rightState === 'indexing' && (
        <div className="flex-1 flex flex-col items-center justify-center gap-3">
          <div className="text-tn-blue text-xs animate-pulse">Building source index…</div>
          <div className="text-tn-dim text-[12px]">This may take a few minutes on first run</div>
        </div>
      )}

      {side === 'cpp' && rightState === 'multiple' && entry.srcCandidates && (
        <div className="flex-1 flex flex-col items-start p-4 gap-3">
          <div className="text-tn-yellow text-sm font-medium flex items-center gap-2">
            <span>⚠</span> Multiple source candidates found
          </div>
          <div className="text-tn-dim text-xs">Select the correct match for <span className="font-mono text-tn-cyan">{entry.funcName}</span>:</div>
          {entry.srcCandidates.map((c) => (
            <button
              key={c}
              onClick={() => onSelectCandidate(c)}
              className="w-full text-left px-3 py-2 bg-tn-surface border border-tn-border rounded text-xs font-mono text-tn-text hover:border-tn-blue hover:bg-tn-surf2"
            >
              {c}
            </button>
          ))}
        </div>
      )}

      {((side === 'ida' && leftState !== 'failed') ||
        (side === 'cpp' && rightState === 'ok')) && (
        <div
          ref={bodyRef}
          onScroll={handleScroll}
          className="flex-1 overflow-auto bg-tn-bg"
        >
          <table className="w-full border-collapse">
            <tbody>
              {lines.map((line, i) => {
                const tokens = tokenLines[i];
                const isMatch = matchIndices.includes(i);
                return (
                  <tr key={i} className={`code-line group ${isMatch ? 'bg-tn-yellow/10' : ''}`}>
                    <td className="select-none text-right pr-4 pl-3 text-tn-dim2 font-mono text-xs leading-5 w-10 align-top whitespace-nowrap">
                      {startLine + i}
                    </td>
                    <td className="pr-6 font-mono text-xs leading-5 whitespace-pre align-top">
                      {tokens.map((tok, j) => (
                        <span key={j} className={tok.cls}>{tok.text}</span>
                      ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
