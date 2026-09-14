import type { ReviewEntry } from '../../data/reviewData';

interface BottomBarProps {
  pinnedEntry: ReviewEntry | null;
  viewEntry: ReviewEntry | null;
  isViewingPinned: boolean;
  saveError: string | null;
  canUndo: boolean;
  canComplete: boolean;
  busy: boolean;
  onPrev: () => void;
  onSkip: () => void;
  onComplete: () => void;
  onUndo: () => void;
  onReturnToPinned: () => void;
}

export default function BottomBar({
  pinnedEntry, viewEntry, isViewingPinned,
  saveError, canUndo, canComplete, busy,
  onPrev, onSkip, onComplete, onUndo, onReturnToPinned,
}: BottomBarProps) {
  const entry = pinnedEntry;

  return (
    <div className="flex-shrink-0 flex items-center gap-3 px-4 h-13 bg-tn-bg2 border-t border-tn-border">
      {/* current task info */}
      <div className="flex-1 min-w-0">
        {entry ? (
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-tn-dim text-xs flex-shrink-0">Reviewing:</span>
            <span className="font-mono text-tn-blue text-xs font-medium truncate">{entry.funcName}</span>
            <span className="text-tn-dim2 text-[10px] flex-shrink-0">{entry.author}</span>
            <span className="text-tn-dim2 text-[10px] flex-shrink-0 font-mono">{entry.version}</span>
            <span className="text-tn-dim2 text-[10px] flex-shrink-0 font-mono">{entry.address}</span>
          </div>
        ) : (
          <span className="text-tn-dim text-xs">No active task</span>
        )}
        {saveError && (
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className="text-tn-red text-[10px]">⚠</span>
            <span className="text-tn-red text-[10px]">{saveError}</span>
          </div>
        )}
      </div>

      {/* return to task if viewing different entry */}
      {!isViewingPinned && pinnedEntry && (
        <button
          onClick={onReturnToPinned}
          className="px-2.5 py-1.5 text-xs border border-tn-yellow/50 rounded text-tn-yellow hover:bg-tn-yellow/10 flex items-center gap-1.5"
        >
          <span>↩</span> Return to task
        </button>
      )}

      {/* actions */}
      <button
        onClick={onPrev}
        disabled={!entry || busy}
        className="px-2.5 py-1.5 text-xs border border-tn-border rounded text-tn-dim hover:text-tn-text hover:bg-tn-surface disabled:opacity-30"
      >
        ‹ Prev
      </button>

      <button
        onClick={onSkip}
        disabled={!entry || busy}
        className="px-2.5 py-1.5 text-xs border border-tn-border rounded text-tn-yellow hover:bg-tn-yellow/10 disabled:opacity-30"
      >
        Skip
      </button>

      <button
        onClick={onUndo}
        disabled={!canUndo || busy}
        className="px-2.5 py-1.5 text-xs border border-tn-border rounded text-tn-dim hover:text-tn-text hover:bg-tn-surface disabled:opacity-30"
        title="Undo last Complete"
      >
        ↩ Undo
      </button>

      <button
        onClick={onComplete}
        disabled={!canComplete || busy}
        className="px-4 py-1.5 text-xs bg-tn-blue text-tn-bg rounded font-semibold hover:bg-tn-blue/90 disabled:opacity-30 flex items-center gap-1.5"
      >
        <span>✓</span> Complete &amp; Next
      </button>
    </div>
  );
}
