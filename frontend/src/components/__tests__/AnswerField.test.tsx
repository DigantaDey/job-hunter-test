/**
 * The typed-answer contract: the control kind follows the field's own type, and
 * the answers payload carries explicit answers only — a checkbox always ships
 * its state ("false" is the answer "no"), everything else ships only non-empty
 * values, and nothing secret-shaped is ever collected.
 */
import { describe, expect, it } from 'vitest'
import { collectAnswers, controlKind, initialAnswerValue, selectOptions } from '../AnswerField'

describe('AnswerField contract', () => {
  it('picks the control by the field type', () => {
    expect(controlKind({ name: 'a', type: 'checkbox' })).toBe('checkbox')
    expect(controlKind({ name: 'a', type: 'radio', options: ['Yes', 'No'] })).toBe('radio')
    expect(controlKind({ name: 'a', type: 'select', options: ['x'] })).toBe('select')
    expect(controlKind({ name: 'a', type: 'date' })).toBe('date')
    expect(controlKind({ name: 'a', type: 'textarea' })).toBe('textarea')
    expect(controlKind({ name: 'a', type: 'email' })).toBe('text')
    // A radio with no revealed options falls back to a picker, not free text.
    expect(controlKind({ name: 'a', type: 'radio' })).toBe('select')
    // Secrets never become a value control here.
    expect(controlKind({ name: 'a', type: 'password' })).toBe('secret')
  })

  it('uses the portal\'s own options, with a Yes/No fallback only when none exist', () => {
    expect(selectOptions({ name: 'a', type: 'select', options: ['she/her', 'they/them'] }))
      .toEqual(['she/her', 'they/them'])
    expect(selectOptions({ name: 'a', type: 'select' })).toEqual(['Yes', 'No'])
  })

  it('collects explicit answers: tick boxes always, text only when filled', () => {
    const fields = [
      { name: 'remote_ok', label: 'Remote?', type: 'checkbox' },
      { name: 'nickname', label: 'Nickname', type: 'text' },
      { name: 'pronouns', label: 'Pronouns', type: 'select', options: ['they/them'] },
      { name: 'password', label: 'Password', type: 'password' },
    ]
    expect(collectAnswers(fields, {
      remote_ok: 'false', nickname: '  ', pronouns: 'they/them', password: 'hunter2',
    })).toEqual({ remote_ok: 'false', pronouns: 'they/them' })
    expect(collectAnswers(fields, { remote_ok: 'true' })).toEqual({ remote_ok: 'true' })
  })

  it('starts a tick box as the explicit "false" and everything else empty', () => {
    expect(initialAnswerValue({ name: 'a', type: 'checkbox' })).toBe('false')
    expect(initialAnswerValue({ name: 'a', type: 'text' })).toBe('')
  })
})
