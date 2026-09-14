import type { ReviewEntry, ReviewStatus } from '../../data/reviewData';

interface FunctionListProps {
  entries: ReviewEntry[];
  currentId: string;
  pinnedId: string;
  collapsed: boolean;
  onSelect: (id: string) => void;
  onToggleCollapse: () => void;
}

const STATUS_ICON: Record<ReviewStatus, string> = {
  pending: '○',
  done:    '✓',
  skipped: '⤳',
};

const STATUS_CLS: Record<ReviewStatus, string> = {
  pending: 'text-tn-dim',
  done:    'text-tn-green',
  skipped: 'text-tn-yellow',
};

const STATUS_LABEL_CLS: Record<ReviewStatus, string> = {
  pending: 'text-tn-text',
  done:    'text-tn-dim line-through decoration-tn-dim2',
  skipped: 'text-tn-dim',
};

export default function FunctionList({
  entries, currentId, pinnedId, collapsed, onSelect, onToggleCollapse,
}: FunctionListProps) {
  const done    = entries.filter((e) => e.status === 'done').length;
  const skipped = entries.filter((e) => e.status === 'skipped').length;
  const total   = entries.length;

  if (collapsed) {
    return (
      <div className="flex flex-col items-center w-9 flex-shrink-0 bg-tn-bg2 border-r border-tn-border py-2 gap-2">
        <button
          onClick={onToggleCollapse}
          className="text-tn-dim hover:text-tn-blue text-base"
          title="Expand function list"
        >
          ›
        </button>
        <div
          className="text-tn-dim text-[10px] font-mono rotate-90 whitespace-nowrap mt-2"
          style={{ writingMode: 'vertical-rl' }}
        >
          {done}/{total}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col w-64 flex-shrink-0 bg-tn-bg2 border-r border-tn-border">
      {/* header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-tn-border flex-shrink-0">
        <div className="text-xs text-tn-dim">
          <span className="text-tn-green font-mono">Done {done}</span>
          <span className="text-tn-dim mx-1">/</span>
          <span className="font-mono">{total}</span>
          {skipped > 0 && (
            <span className="text-tn-yellow ml-1 font-mono">· Skipped {skipped}</span>
          )}
        </div>
        <button
          onClick={onToggleCollapse}
          className="text-tn-dim hover:text-tn-blue text-sm"
          title="Collapse list"
        >
          ‹
        </button>
      </div>

      {/* progress bar */}
      <div className="h-0.5 bg-tn-border flex-shrink-0">
        <div
          className="h-full bg-tn-green transition-all"
          style={{ width: total > 0 ? `${(done / total) * 100}%` : '0%' }}
        />
      </div>

      {/* list */}
      <div className="flex-1 overflow-y-auto">
        {entries.length === 0 ? (
          <div className="text-tn-dim text-xs px-3 py-4 text-center">No functions match filters</div>
        ) : (
          entries.map((entry) => {
            const isCurrent = entry.id === currentId;
            const isPinned  = entry.id === pinnedId && entry.id !== currentId;
            return (
              <button
                key={entry.id}
                onClick={() => onSelect(entry.id)}
                className={`w-full text-left px-3 py-2 border-b border-tn-border/40 hover:bg-tn-surface transition-colors ${
                  isCurrent ? 'bg-tn-surface border-l-2 border-l-tn-blue' : ''
                } ${isPinned ? 'border-l-2 border-l-tn-yellow' : ''}`}
              >
                <div className="flex items-start gap-1.5">
                  <span className={`flex-shrink-0 mt-0.5 font-mono text-xs ${STATUS_CLS[entry.status]}`}>
                    {STATUS_ICON[entry.status]}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className={`text-xs font-medium truncate font-mono ${STATUS_LABEL_CLS[entry.status]} ${isCurrent ? 'text-tn-blue' : ''}`}>
                      {entry.funcName}
                    </div>
                    <div className="text-[10px] text-tn-dim font-mono mt-0.5 flex items-center gap-1">
                      <span>{entry.module}</span>
                      <span className="text-tn-dim2">·</span>
                      <span>{entry.address}</span>
                    </div>
                    {entry.author && (
                      <div className="text-[10px] text-tn-dim2 mt-0.5">{entry.author}</div>
                    )}
                  </div>
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
