import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/**
 * Component tests (`npm run test`).
 *
 * A separate config on purpose: `vite.config.ts` stays free of any test-only
 * dependency, so `npm run build` — including the production image build — never
 * needs vitest installed. The tests are hermetic: the API client is mocked, so
 * nothing reaches a backend or the network.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    restoreMocks: true,
    clearMocks: true,
  }
})
