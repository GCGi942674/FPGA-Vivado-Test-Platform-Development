import type { IdaStatus } from '../../data/reviewData';


interface TopBarProps {
  versions: string[];
  authors: string[];
  modules: string[];
  version: string;
  author: string;
  module: string;
  statusFilter: string;
  searchQuery: string;
  idaStatus: IdaStatus;
  idaDb: string;
  onVersion: (v: string) => void;
  onAuthor: (a: string) => void;
  onModule: (m: string) => void;
  onStatusFilter: (s: string) => void;
  onSearch: (q: string) => void;
  onReconnect: () => void;
  onOpenSettings: () => void;
}

const STATUS_OPTS = ['All', 'Pending', 'Done', 'Skipped'];

const IDA_DOT: Record<IdaStatus, string> = {
  connected: 'bg-tn-green',
  disconnected: 'bg-tn-red',
  connecting: 'bg-tn-yellow animate-pulse',
};

const IDA_LABEL: Record<IdaStatus, string> = {
  connected: 'IDA Connected',
  disconnected: 'IDA Disconnected',
  connecting: 'Connecting…',
};

export default function TopBar({
  versions, authors, modules, version, author, module: mod, statusFilter, searchQuery,
  idaStatus, idaDb,
  onVersion, onAuthor, onModule, onStatusFilter, onSearch,
  onReconnect, onOpenSettings,
}: TopBarProps) {
  return (
    <div className="flex items-center gap-2 px-3 min-h-10 py-2 flex-wrap bg-tn-bg2 border-b border-tn-border flex-shrink-0 select-none">
      {/* Brand */}
      <span className="text-xs font-semibold text-tn-blue tracking-widest uppercase mr-1">Review</span>
      <div className="w-px h-4 bg-tn-border mx-1" />

      {/* Filters */}
      <label className="text-tn-dim text-xs">Version</label>
      <select
        value={version}
        onChange={(e) => onVersion(e.target.value)}
        className="bg-tn-surf2 border border-tn-border text-tn-text text-xs rounded px-1.5 py-0.5 focus:outline-none focus:border-tn-blue"
      >
        {['All', ...versions].map((v) => <option key={v}>{v}</option>)}
      </select>

      <label className="text-tn-dim text-xs">Author</label>
      <select
        value={author}
        onChange={(e) => onAuthor(e.target.value)}
        className="bg-tn-surf2 border border-tn-border text-tn-text text-xs rounded px-1.5 py-0.5 focus:outline-none focus:border-tn-blue"
      >
        <option value="All">All authors</option>
        {authors.map((a) => <option key={a}>{a}</option>)}
      </select>

      <label className="text-tn-dim text-xs">Module</label>
      <select
        value={mod}
        onChange={(e) => onModule(e.target.value)}
        className="bg-tn-surf2 border border-tn-border text-tn-text text-xs rounded px-1.5 py-0.5 focus:outline-none focus:border-tn-blue"
      >
        <option value="All">All modules</option>
        {modules.map((m) => <option key={m}>{m}</option>)}
      </select>

      <label className="text-tn-dim text-xs">Status</label>
      <select
        value={statusFilter}
        onChange={(e) => onStatusFilter(e.target.value)}
        className="bg-tn-surf2 border border-tn-border text-tn-text text-xs rounded px-1.5 py-0.5 focus:outline-none focus:border-tn-blue"
      >
        {STATUS_OPTS.map((s) => <option key={s}>{s}</option>)}
      </select>

      {/* Search */}
      <div className="relative ml-1">
        <span className="absolute left-2 top-1/2 -translate-y-1/2 text-tn-dim text-xs pointer-events-none">⌕</span>
        <input
          value={searchQuery}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Function name or address…"
          className="bg-tn-surf2 border border-tn-border text-tn-text text-xs rounded pl-5 pr-2 py-0.5 w-48 focus:outline-none focus:border-tn-blue placeholder-tn-dim"
        />
      </div>

      <div className="flex-1" />

      {/* IDA status */}
      <button
        onClick={onReconnect}
        title="Refresh IDA connections"
        className="flex items-center gap-1.5 text-xs text-tn-dim hover:text-tn-text"
      >
        <span className={`w-2 h-2 rounded-full flex-shrink-0 ${IDA_DOT[idaStatus]}`} />
        <span>{IDA_LABEL[idaStatus]}</span>
        {idaStatus === 'connected' && (
          <span className="text-tn-dim2 font-mono ml-1">{idaDb}</span>
        )}
      </button>

      <div className="w-px h-4 bg-tn-border mx-1" />

      {/* Settings */}
      <button
        onClick={onOpenSettings}
        className="text-tn-dim hover:text-tn-text text-xs flex items-center gap-1"
        title="Project Settings"
      >
        <span>⚙</span>
        <span>Settings</span>
      </button>
    </div>
  );
}
