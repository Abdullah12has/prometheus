import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 4310,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:4311',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: {
    port: 4310,
    strictPort: true,
  },
})
