export type ReviewStatus = 'pending' | 'done' | 'skipped';
export type IdaStatus = 'connected' | 'disconnected' | 'connecting';
export type LeftPaneState = 'ok' | 'loading' | 'stale' | 'failed';
export type RightPaneState = 'ok' | 'loading' | 'not-found' | 'multiple' | 'indexing';

export interface ReviewEntry {
  id: string;
  author: string;
  version: string;
  address: string;
  module: string;
  funcName: string;
  status: ReviewStatus;
  // Source address mapping parsed from C++ comment
  srcAddress: string;
  srcModule: string;
  targetId: string;
  srcFile: string;
  srcLine: number;
  pseudocode: string;
  sourceCode: string;
  srcCandidates?: string[];
}
