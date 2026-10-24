// The session records as the server's OpenAPI schema describes them
// (`schema.d.ts`, generated from `llm-switchboard openapi`); these names are
// what the rest of the page imports.
import type { components } from './schema'

type Schemas = components['schemas']

export const TOOLS = { claude: 'claude', codex: 'codex' } as const
export type SessionRow = Schemas['SessionRow']
export type SessionTool = SessionRow['tool']
export type SessionStatus = Schemas['SessionStatus']
export type SupervisorState = Schemas['State']
export type SessionExit = Schemas['ExitSummary']
export type OpenProcess = Schemas['OpenProcess']
export type HostAvailability = Schemas['HostStatus']
export type HostSessionState = Schemas['SessionState']
export type HostActionState = Schemas['ActionState']
// The schema leaves Codex's thread state a plain string (it comes from Codex);
// these are the values the page acts on.
export type CodexState = 'notLoaded' | 'idle' | 'active' | 'systemError'
