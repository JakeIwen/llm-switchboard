import { PickerPage } from './pages/PickerPage'
import { useThemeColor } from './styles/useThemeColor'

export function App() {
  useThemeColor()
  return <PickerPage />
}
