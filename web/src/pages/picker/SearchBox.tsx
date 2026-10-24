import { forwardRef } from 'react'
import { ActionIcon, CloseButton, Popover, TextInput } from '@mantine/core'
import { IconFileText, IconInfoCircle, IconSearch } from '@tabler/icons-react'
import { KEYBOARD_HELP, SESSION_SEARCH_HELP, TRANSCRIPT_SEARCH_HELP, type HelpLine } from '../../content/searchHelp'
import './SearchBox.css'

export type SearchMode = 'sessions' | 'transcripts'

function HelpList({ title, lines }: { title: string; lines: readonly HelpLine[] }) {
  return (
    <section className="search-help-section">
      <h3>{title}</h3>
      <dl>
        {lines.map(([term, meaning]) => (
          <div key={term} className="search-help-line">
            <dt>
              <code>{term}</code>
            </dt>
            <dd>{meaning}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

interface Props {
  value: string
  mode: SearchMode
  onChange: (value: string) => void
  onSubmit: () => void
  onEscape: () => void
}

export const SearchBox = forwardRef<HTMLInputElement, Props>(function SearchBox({ value, mode, onChange, onSubmit, onEscape }, ref) {
  const transcripts = mode === 'transcripts'
  return (
    <TextInput
      ref={ref}
      className="search-box"
      type="search"
      value={value}
      placeholder={transcripts ? 'Search inside transcripts' : 'Search sessions'}
      aria-label={transcripts ? 'Search inside transcripts' : 'Search sessions'}
      autoComplete="off"
      spellCheck={false}
      enterKeyHint="search"
      leftSection={transcripts ? <IconFileText size={16} /> : <IconSearch size={16} />}
      rightSectionWidth={value ? 60 : 32}
      rightSection={
        <div className="search-box-right">
          {value && <CloseButton size="sm" aria-label="Clear search" onClick={() => onChange('')} />}
          <Popover width={340} position="bottom-end">
            <Popover.Target>
              <ActionIcon size="sm" aria-label="Search syntax">
                <IconInfoCircle size={16} />
              </ActionIcon>
            </Popover.Target>
            <Popover.Dropdown className="search-help">
              <HelpList title="Sessions: filters as you type" lines={SESSION_SEARCH_HELP} />
              <HelpList title="Transcripts: Enter searches inside prompts and replies" lines={TRANSCRIPT_SEARCH_HELP} />
              <p className="search-help-keys">{KEYBOARD_HELP}</p>
            </Popover.Dropdown>
          </Popover>
        </div>
      }
      onChange={(e) => onChange(e.currentTarget.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSubmit()
        if (e.key === 'Escape') onEscape()
      }}
    />
  )
})
