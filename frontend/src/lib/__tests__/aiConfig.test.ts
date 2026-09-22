/**
 * Per-workflow override form helpers.
 *
 * The admin section used to render inputs only for keys already present in
 * `overrides`. A fresh workspace returns `overrides: {}`, so the heading had
 * no fields under it and there was no way to create one. These tests pin the
 * catalog (always a list of inputs) and the payload the Save button posts.
 */
import { describe, expect, it } from 'vitest'
import {
  AI_WORKFLOWS,
  looksMasked,
  overrideSaveError,
  overrideSavePayload,
  workflowCatalog,
} from '../aiConfig'

describe('workflow override catalog', () => {
  it('falls back to every known workflow when the server has no overrides and no list', () => {
    const ids = workflowCatalog({ overrides: {} }).map(row => row.id)
    expect(ids).toEqual([...AI_WORKFLOWS])
    expect(ids).toContain('scoring')
    expect(ids).toContain('persona')
  })

  it('uses the server workflow map, including descriptions, when one is sent', () => {
    const catalog = workflowCatalog({
      workflows: { scoring: 'profile scoring', resume_gen: 'tailored resumes' },
      overrides: {},
    })
    expect(catalog).toEqual([
      { id: 'scoring', description: 'profile scoring' },
      { id: 'resume_gen', description: 'tailored resumes' },
    ])
  })

  it('keeps a saved override editable even if it is missing from the workflow list', () => {
    const ids = workflowCatalog({
      workflows: { scoring: 'scoring' },
      overrides: { persona: { model: 'gpt-4o' } },
    }).map(row => row.id)
    expect(ids).toEqual(['scoring', 'persona'])
  })
})

describe('workflow override save payload', () => {
  it('trims fields and leaves a blank key blank so the server keeps the stored one', () => {
    expect(overrideSavePayload({
      base_url: '  https://api.openai.com/v1  ',
      model: ' gpt-4o ',
      provider: 'openai',
      api_key: '',
      api_key_set: true,
    })).toEqual({
      base_url: 'https://api.openai.com/v1',
      model: 'gpt-4o',
      provider: 'openai_compatible',
      api_key: '',
    })
  })

  it('refuses an empty draft — a provider alone is deleted by the API, not saved', () => {
    expect(overrideSaveError({})).toMatch(/base URL, model, or API key/i)
    expect(overrideSaveError({ provider: 'google' })).toMatch(/base URL, model, or API key/i)
    expect(overrideSavePayload({})).toEqual({ base_url: '', model: '', provider: '', api_key: '' })
  })

  it('refuses a base URL the gateway would reject, and masked placeholder keys', () => {
    expect(overrideSaveError({ base_url: 'api.openai.com', model: 'gpt-4o' })).toMatch(/http/)
    expect(overrideSaveError({ model: 'x', api_key: 'sk-real' })).toMatch(/2 characters/)
    expect(looksMasked('***')).toBe(true)
    expect(looksMasked('••••')).toBe(true)
    expect(overrideSaveError({ model: 'gpt-4o', api_key: '••••' })).toMatch(/masked placeholder/)
  })

  it('accepts a real override, including one that only changes the model', () => {
    expect(overrideSaveError({ model: 'gpt-4o-mini' })).toBeNull()
    expect(overrideSaveError({
      base_url: 'https://api.groq.com/openai/v1',
      model: 'llama-3.1-70b',
      api_key: 'gsk-live-key',
      provider: 'openai_compatible',
    })).toBeNull()
    // A saved key with every visible field cleared is still an override.
    expect(overrideSaveError({ api_key_set: true })).toBeNull()
  })
})
