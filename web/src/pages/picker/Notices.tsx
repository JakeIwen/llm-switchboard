import './Notices.css'

/** One quiet line under the toolbar for problems with the query or the
    service; nothing at all when there are none. */
export function Notices({ lines }: { lines: readonly string[] }) {
  if (lines.length === 0) return null
  return (
    <p className="notices" role="status">
      {lines.join(' · ')}
    </p>
  )
}
