/**
 * Per-workflow overrides on the admin console.
 *
 * With no saved overrides, the section used to render a sentence and nothing
 * else — no base URL, model, provider or key field — so an owner could not
 * create one. These tests pin the inputs, the POST the Save button actually
 * sends, and the DELETE Clear uses.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import client from '../../api/client'

vi.mock('../../api/client', () => {
  const get = vi.fn()
  const post = vi.fn()
  const put = vi.fn()
  const del = vi.fn()
  return {
    default: { get, post, put, delete: del },
    apiError: (e: unknown, fb?: string) => {
      const err = e as { response?: { data?: { detail?: { message?: string } | string } } }
      const detail = err?.response?.data?.detail
      if (typeof detail === 'string') return detail
      if (detail && typeof detail === 'object' && detail.message) return detail.message
      return fb ?? 'error'
    },
  }
})

import AdminConsole from '../admin/AdminConsole'

const get = client.get as unknown as Mock
const post = client.post as unknown as Mock
const del = client.delete as unknown as Mock

const WORKFLOWS = {
  scoring: 'profile scoring',
  resume_gen: 'tailored resume generation',
}

function configPayload(overrides: Record<string, unknown> = {}) {
  return { workflows: WORKFLOWS, overrides }
}

function installGets(overrides: Record<string, unknown> = {}) {
  get.mockImplementation((url: string) => {
    if (url === '/api/ai/config') return Promise.resolve({ data: configPayload(overrides) })
    if (url === '/api/settings') {
      return Promise.resolve({ data: {
        ai: {
          base_url: 'https://api.openai.com/v1',
          model: 'gpt-4o-mini',
          api_key_set: true,
          rpm: 60,
          timeout: 300,
          max_input_tokens: 0,
          max_output_tokens: 0,
          provider: 'openai_compatible',
          token_limits_editable: true,
          platform_max_input_tokens: 0,
          platform_max_output_tokens: 0,
        },
      } })
    }
    if (url === '/api/admin/overview') {
      return Promise.resolve({ data: {
        queues: { totals: { queued: 0 } },
        usage_cost: {},
        failure_rates: {},
        automation_health: {},
        accounts: {},
        sources: { job_sources: [] },
      } })
    }
    if (url === '/api/admin/flags') return Promise.resolve({ data: { flags: [] } })
    return Promise.resolve({ data: {} })
  })
}

describe('Admin console: per-workflow overrides', () => {
  beforeEach(() => {
    get.mockReset()
    post.mockReset()
    del.mockReset()
    post.mockResolvedValue({ data: { ok: true } })
    del.mockResolvedValue({ data: { ok: true } })
  })
  afterEach(cleanup)

  it('shows an input for every workflow when no override has been saved yet', async () => {
    installGets({})
    render(<MemoryRouter><AdminConsole /></MemoryRouter>)

    await waitFor(() => expect(screen.getByTestId('workflow-overrides')).toBeTruthy())

    expect(screen.getByText(/No overrides saved yet/)).toBeTruthy()
    // The old empty state replaced the form. Inputs must be here anyway.
    expect(screen.getByLabelText('Base URL for scoring')).toBeTruthy()
    expect(screen.getByLabelText('Model for scoring')).toBeTruthy()
    expect(screen.getByLabelText('Provider for scoring')).toBeTruthy()
    expect(screen.getByLabelText('API key for scoring')).toBeTruthy()
    expect(screen.getByLabelText('Base URL for resume_gen')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save override for scoring' })).toBeTruthy()
    expect((screen.getByLabelText('Provider for scoring') as HTMLSelectElement).value).toBe('')
    const provider = screen.getByLabelText('Provider for scoring') as HTMLSelectElement
    expect(Array.from(provider.options).map(option => option.text)).toEqual([
      'Inherit default',
      'OpenAI compatible',
      'Google AI Studio (Gemini)',
    ])
  })

  it('posts the filled override and reloads it as saved', async () => {
    let overrides: Record<string, unknown> = {}
    get.mockImplementation((url: string) => {
      if (url === '/api/ai/config') return Promise.resolve({ data: configPayload(overrides) })
      if (url === '/api/settings') return Promise.resolve({ data: { ai: { base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini', rpm: 60, timeout: 300, max_input_tokens: 0, max_output_tokens: 0, provider: 'openai_compatible', token_limits_editable: true } } })
      if (url === '/api/admin/overview') return Promise.resolve({ data: { queues: { totals: {} }, sources: { job_sources: [] } } })
      if (url === '/api/admin/flags') return Promise.resolve({ data: { flags: [] } })
      return Promise.resolve({ data: {} })
    })
    post.mockImplementation((url: string, body: Record<string, any>) => {
      if (url === '/api/ai/config') {
        const [wf, cfg] = Object.entries(body)[0]
        overrides = {
          ...overrides,
          [wf]: { ...cfg, api_key: '', api_key_set: Boolean(cfg.api_key) },
        }
      }
      return Promise.resolve({ data: { ok: true } })
    })

    render(<MemoryRouter><AdminConsole /></MemoryRouter>)
    await waitFor(() => expect(screen.getByLabelText('Model for scoring')).toBeTruthy())

    fireEvent.change(screen.getByLabelText('Base URL for scoring'), { target: { value: 'https://api.groq.com/openai/v1' } })
    fireEvent.change(screen.getByLabelText('Model for scoring'), { target: { value: 'llama-3.1-70b' } })
    fireEvent.change(screen.getByLabelText('Provider for scoring'), { target: { value: 'openai_compatible' } })
    fireEvent.change(screen.getByLabelText('API key for scoring'), { target: { value: 'gsk-live-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save override for scoring' }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/ai/config', {
      scoring: {
        base_url: 'https://api.groq.com/openai/v1',
        model: 'llama-3.1-70b',
        provider: 'openai_compatible',
        api_key: 'gsk-live-key',
      },
    }))
    await waitFor(() => expect(screen.getByText('override saved')).toBeTruthy())
    expect((screen.getByLabelText('Model for scoring') as HTMLInputElement).value).toBe('llama-3.1-70b')
    expect((screen.getByLabelText('API key for scoring') as HTMLInputElement).value).toBe('')
    expect(screen.getByText('A key is saved for this workflow.')).toBeTruthy()
  })

  it('does not post an empty override, and Clear deletes a saved one', async () => {
    installGets({
      resume_gen: { base_url: 'https://api.example.com/v1', model: 'gpt-4o', provider: '', api_key: '', api_key_set: true },
    })
    render(<MemoryRouter><AdminConsole /></MemoryRouter>)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Clear override for resume_gen' })).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: 'Save override for scoring' }))
    await waitFor(() => expect(screen.getByTestId('admin-notice').textContent).toMatch(/base URL, model, or API key/i))
    expect(post).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Clear override for resume_gen' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/api/ai/config/resume_gen'))
  })

  it('shows the server error when the save is rejected', async () => {
    installGets({})
    post.mockRejectedValue({ response: { data: { detail: { message: 'base_url must be a valid URL' } } } })
    render(<MemoryRouter><AdminConsole /></MemoryRouter>)
    await waitFor(() => expect(screen.getByLabelText('Model for scoring')).toBeTruthy())

    fireEvent.change(screen.getByLabelText('Model for scoring'), { target: { value: 'gpt-4o' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save override for scoring' }))

    await waitFor(() => expect(screen.getByTestId('admin-notice').textContent).toContain('base_url must be a valid URL'))
  })
})
