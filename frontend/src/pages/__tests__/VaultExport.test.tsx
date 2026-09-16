/**
 * Vault CSV downloads must be authenticated (the import fix for P0 #1).
 *
 * The buttons used to be bare `<a href="/api/vault/export/chrome">` links, so
 * a click navigated without the Authorization header and the backend answered
 * `{"detail":"Not authenticated"}` instead of a CSV. They now go through
 * `downloadVaultCsv()`, which fetches a short-lived signed URL through the
 * authenticated client and hands it to `window.location.assign` — the same
 * pattern `downloadResume()` already uses for resume downloads. These tests
 * pin the half of the fix that lives in the component: the buttons are real
 * buttons routed through the helper, not bare unauthenticated hrefs.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import client, { downloadVaultCsv } from '../../api/client'
import Vault from '../Vault'

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), delete: vi.fn() },
  downloadVaultCsv: vi.fn(),
}))

const get = client.get as unknown as Mock
const downloadVaultCsvMock = downloadVaultCsv as unknown as Mock

afterEach(cleanup)

describe('Vault: authenticated CSV downloads', () => {
  beforeEach(() => {
    get.mockReset()
    downloadVaultCsvMock.mockReset()
    // `downloadVaultCsv` navigates once it has the signed URL — mirror its
    // real behaviour so the click completes and we can assert on it.
    downloadVaultCsvMock.mockImplementation(async (flavour: string) => {
      window.location.assign(`/api/vault/export/${flavour}?token=abc.1.sig`)
      return `jobhunter_${flavour}_passwords.csv`
    })
    get.mockImplementation((url: string) => {
      if (url === '/api/vault') return Promise.resolve({ data: [] })
      return Promise.resolve({ data: [] })
    })
  })

  it('downloads Chrome CSV through the authenticated helper, not a bare href', async () => {
    render(<Vault />)
    await act(async () => { await Promise.resolve() })

    const button = screen.getByRole('button', { name: /Download Chrome CSV/i })
    expect(button.tagName).toBe('BUTTON')
    expect(button.getAttribute('href')).toBeNull()

    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign, href: 'http://localhost/' } })
    fireEvent.click(button)
    await act(async () => { await Promise.resolve() })

    expect(downloadVaultCsvMock).toHaveBeenCalledWith('chrome')
    expect(assign).toHaveBeenCalledWith('/api/vault/export/chrome?token=abc.1.sig')
    // The bare-anchor regression is an <a> with no click handler.
    expect(screen.queryByRole('link', { name: /Download Chrome CSV/i })).toBeNull()
  })

  it('downloads Apple Keychain CSV through the authenticated helper', async () => {
    render(<Vault />)
    await act(async () => { await Promise.resolve() })

    const button = screen.getByRole('button', { name: /Download Apple Keychain CSV/i })
    const assign = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { assign, href: 'http://localhost/' } })
    fireEvent.click(button)
    await act(async () => { await Promise.resolve() })

    expect(downloadVaultCsvMock).toHaveBeenCalledWith('apple')
    expect(assign).toHaveBeenCalledWith('/api/vault/export/apple?token=abc.1.sig')
    expect(screen.queryByRole('link', { name: /Download Apple Keychain CSV/i })).toBeNull()
  })
})
