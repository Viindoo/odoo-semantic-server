// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for the form-field validators.
 *
 * Convention under protection: a validator returns `null` when VALID and a
 * non-empty error string when INVALID. Each validator gets one passing and one
 * failing case so the test fails iff the rule it guards is broken.
 */

import { describe, expect, it } from 'vitest';
import {
  commonPwBlocklist,
  confirmMatch,
  email,
  minLength,
  nonNegInt,
  otpDigits,
  trimNonEmpty,
} from '../validators';

describe('minLength', () => {
  it('passes a string at or above the minimum', () => {
    expect(minLength(8)('abcdefgh')).toBeNull();
  });
  it('fails a string below the minimum', () => {
    expect(minLength(8)('short')).toMatch(/at least 8/);
  });
});

describe('confirmMatch', () => {
  it('passes when both values are equal', () => {
    expect(confirmMatch('secret', 'secret')).toBeNull();
  });
  it('fails when values differ', () => {
    expect(confirmMatch('secret', 'other')).not.toBeNull();
  });
});

describe('otpDigits', () => {
  it('passes exactly 6 digits', () => {
    expect(otpDigits('123456')).toBeNull();
  });
  it('fails when not 6 digits', () => {
    expect(otpDigits('12345')).not.toBeNull();
    expect(otpDigits('abcdef')).not.toBeNull();
  });
});

describe('nonNegInt', () => {
  it('passes a non-negative integer string', () => {
    expect(nonNegInt('0')).toBeNull();
    expect(nonNegInt('42')).toBeNull();
  });
  it('fails a negative or non-integer string', () => {
    expect(nonNegInt('-1')).not.toBeNull();
    expect(nonNegInt('3.5')).not.toBeNull();
  });
});

describe('trimNonEmpty', () => {
  it('passes a non-blank string', () => {
    expect(trimNonEmpty('  hi  ')).toBeNull();
  });
  it('fails a blank / whitespace-only string', () => {
    expect(trimNonEmpty('   ')).not.toBeNull();
  });
});

describe('email', () => {
  it('passes a plausible address', () => {
    expect(email('user@example.com')).toBeNull();
  });
  it('fails a malformed address', () => {
    expect(email('not-an-email')).not.toBeNull();
  });
});

describe('commonPwBlocklist', () => {
  it('passes a non-listed password', () => {
    expect(commonPwBlocklist('a-rather-unique-phrase-9f2')).toBeNull();
  });
  it('fails a common password (case-insensitive)', () => {
    expect(commonPwBlocklist('Password123')).not.toBeNull();
  });
});
