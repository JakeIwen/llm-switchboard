import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// The page is served at /next/ beside the old one until the swap.
const base = process.env.SWITCHBOARD_BASE ?? '/next/'
// The API the dev server forwards to: by default the Pi's service through
// `ssh -N -L 8777:127.0.0.1:8777 <pi>`, or a fixture instance.
const apiTarget = process.env.SWITCHBOARD_API ?? 'http://127.0.0.1:8777'
// Live data is read-only unless SWITCHBOARD_WRITES=1 (fixture instances only),
// so a test click can't act on a real session.
const allowWrites = process.env.SWITCHBOARD_WRITES === '1'

function refuseWrites(): Plugin {
  return {
    name: 'switchboard-refuse-writes',
    configureServer(server) {
      server.middlewares.use('/api', (req, res, next) => {
        if (allowWrites || req.method === 'GET' || req.method === 'HEAD') return next()
        res.statusCode = 405
        res.setHeader('Content-Type', 'application/json')
        res.end(JSON.stringify({ error: `Read-only dev proxy: ${req.method} refused (set SWITCHBOARD_WRITES=1 for a fixture instance)` }))
      })
    },
  }
}

export default defineConfig({
  base,
  plugins: [refuseWrites(), react()],
  server: {
    host: '127.0.0.1',
    port: Number(process.env.SWITCHBOARD_DEV_PORT ?? 5173),
    strictPort: true,
    proxy: { '/api': { target: apiTarget, changeOrigin: false } },
  },
  build: { sourcemap: false },
})
