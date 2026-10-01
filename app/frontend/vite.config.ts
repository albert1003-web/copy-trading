import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, run `mvn spring-boot:run` (port 8787) alongside `npm run dev`; /api is proxied to it.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': 'http://127.0.0.1:8787' },
  },
})
