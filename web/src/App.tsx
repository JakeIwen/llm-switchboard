import { useEffect, useState } from 'react'
import { Button, Container, Text, Title } from '@mantine/core'

export function App() {
  const [count, setCount] = useState<number | null>(null)
  useEffect(() => {
    void fetch('/api/sessions')
      .then((r) => r.json())
      .then((d: { sessions: unknown[] }) => setCount(d.sessions.length))
  }, [])
  return (
    <Container size="sm" py="md">
      <Title order={1} size="h3">LLM Switchboard</Title>
      <Text c="dimmed" size="sm">{count === null ? 'loading…' : `${count} sessions`}</Text>
      <Button mt="sm">New session</Button>
    </Container>
  )
}
