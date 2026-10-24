// Response records from the server's OpenAPI schema (`schema.d.ts`).
import type { components } from './schema'

type Schemas = components['schemas']

/** `GET /api/sessions` */
export type SessionsPayload = Schemas['SessionsResponse']
export type HostStatus = Schemas['HostStatus']
export type HostAction = Schemas['QueuedAction']
export type FolderSettings = Schemas['FolderSettings']
export type InstallSettings = Schemas['Settings']
/** One `GET /api/trash` entry: a Pi entry, or a Mac session row with `deleted_at`. */
export type TrashItem = Schemas['TrashResponse']['trash'][number]
