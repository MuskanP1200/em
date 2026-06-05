# ================================================================================
# FILE: matching.py
# ================================================================================
import re

import numpy as np
from typing import Dict, Set, Tuple, Optional


def normalize_for_vin_match(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def normalize_for_odometer_match(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    if not isinstance(s, str):
        s = str(int(float(s)))
    unit_match = re.search(r"(\d[\d,]+)\s*\b(miles|mi|km)\b", s, flags=re.IGNORECASE)
    if unit_match:
        s = unit_match.group(1)

    cleaned = re.sub(r"\b(miles|mi|km)\b", "", s, flags=re.IGNORECASE)
    cleaned = re.sub(r"[,\s\.\|]", "", cleaned)

    match = re.search(r"\d+", cleaned)
    return int(match.group()) if match else None


def find_best_match(text_norm_source, target_norm):
    # Guard clauses for edge cases
    if not isinstance(text_norm_source, str) or not isinstance(target_norm, str):
        raise TypeError("Both text_norm_source and target_norm must be strings.")
    if len(target_norm) == 0:
        return ""  # or None, depending on your desired behavior
    if len(text_norm_source) < len(target_norm):
        return None

    # Slide window & score matches (character-by-character agreement)
    best_score = -1
    best_start = None
    target_len = len(target_norm)

    for i in range(0, len(text_norm_source) - target_len + 1):
        candidate = text_norm_source[i : i + target_len]
        score = sum(1 for a, b in zip(candidate, target_norm) if a == b)
        if score > best_score:
            best_score = score
            best_start = i

    best_match_string = (
        text_norm_source[best_start : best_start + target_len]
        if best_start is not None
        else None
    )

    # Store best-match string in a consistent casing (existing behavior uppercases)
    return best_match_string if best_match_string is None else best_match_string.upper()


def calculate_edit_distance(reference, hypothesis):
    """Calculate Character Error Rate between two strings"""

    reference = str(reference)
    hypothesis = str(hypothesis)

    # Calculate Levenshtein distance
    len_ref = len(reference)
    len_hyp = len(hypothesis)

    # Create distance matrix
    d = np.zeros((len_ref + 1, len_hyp + 1))

    for i in range(len_ref + 1):
        d[i][0] = i
    for j in range(len_hyp + 1):
        d[0][j] = j

    for i in range(1, len_ref + 1):
        for j in range(1, len_hyp + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                cost = 0
            else:
                cost = 1

            d[i][j] = min(
                d[i - 1][j] + 1,  # deletion
                d[i][j - 1] + 1,  # insertion
                d[i - 1][j - 1] + cost,  # substitution
            )

    # CER is the edit distance divided by the length of reference
    return d[len_ref][len_hyp] if len_ref > 0 else 0


# --- VIN checksum utilities (ISO 3779) ---
# Positional weights for 17 positions (9th char has weight 0 = check digit position)
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

# Transliteration map per ISO 3779. I, O, Q are invalid in VINs.
_VIN_TRANS: Dict[str, int] = {
    **{str(d): d for d in range(10)},
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "P": 7,
    "R": 9,
    "S": 2,
    "T": 3,
    "U": 4,
    "V": 5,
    "W": 6,
    "X": 7,
    "Y": 8,
    "Z": 9,
}

# Reverse mapping: transliteration value -> allowed characters (excluding I, O, Q)
# 0 maps only to '0'. 10 is not used for regular positions (only check digit can be 'X' representing 10).
_VIN_VALUE_TO_CHARS: Dict[int, Set[str]] = {
    0: {"0"},
    1: {"1", "A", "J"},
    2: {"2", "B", "K", "S"},
    3: {"3", "C", "L", "T"},
    4: {"4", "D", "M", "U"},
    5: {"5", "E", "N", "V"},
    6: {"6", "F", "W"},
    7: {"7", "G", "P", "X"},
    8: {"8", "H", "Y"},
    9: {"9", "R", "Z"},
    # 10 intentionally omitted (no regular char maps to 10; only check digit may be 'X')
}


def _vin_clean(v: Optional[str]) -> str:
    return (v or "").strip().upper()


def _modinv(a: int, m: int = 11) -> Optional[int]:
    """Multiplicative inverse of a mod m (m=11 prime here)."""
    a %= m
    for x in range(1, m):
        if (a * x) % m == 1:
            return x
    return None


# Precompute inverses mod 11 for the usable weights (except 0 at index 8)
_INV_WEIGHT_BY_INDEX: Dict[int, Optional[int]] = {
    i: (_modinv(w) if w != 0 else None) for i, w in enumerate(_VIN_WEIGHTS)
}


def _vin_values(v: str) -> Optional[list]:
    """
    Transliterates VIN-like string to numeric values per char.
    Returns list[17] of ints or None if length != 17.
    Invalid chars yield None entries at those indices.
    """
    v = _vin_clean(v)
    if len(v) != 17:
        return None
    vals = []
    for ch in v:
        vals.append(_VIN_TRANS.get(ch))  # None if invalid char
    return vals


def compute_vin_check_digit(vin: str) -> Optional[str]:
    """
    Computes expected check digit ('0'-'9' or 'X') from other positions.
    Returns None if input length != 17 or has wholly invalid structure.
    """
    v = _vin_clean(vin)
    if len(v) != 17:
        return None
    vals = _vin_values(v)
    if vals is None:
        return None

    # Sum excluding check-digit contribution (weight at index 8 is 0 anyway)
    total = 0
    for i, (val, w) in enumerate(zip(vals, _VIN_WEIGHTS)):
        if val is None:
            # If any non-check-digit char is invalid, we cannot compute reliably
            if i != 8:
                return None
            # If check digit char invalid, it's fine; weight is 0
            continue
        total += val * w

    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def is_valid_vin(vin: str) -> bool:
    """
    VIN must be 17 chars, cannot contain I/O/Q, and 9th char must match computed check digit.
    """
    v = _vin_clean(vin)
    if len(v) != 17:
        return False
    expected = compute_vin_check_digit(v)
    return (expected is not None) and (v[8] == expected)


def possible_checksum_substitutions(candidate: str) -> Dict[int, Set[str]]:
    """
    For a 17-char candidate, return a map {index -> set of replacement chars}
    such that replacing exactly one char at that index with any char in the set
    yields a checksum-valid VIN (ISO 3779).
    - Does NOT include the original character (true substitutions only).
    - For index 8 (check digit), the set is either {expected_digit_or_X} if it's wrong, else empty.
    - If check digit char is invalid (not 0-9 or X), only index 8 will be suggested.
    """
    v = _vin_clean(candidate)
    subs: Dict[int, Set[str]] = {}
    if len(v) != 17:
        return subs

    vals = _vin_values(v)
    if vals is None:
        return subs

    # Compute total sum ignoring check-digit (weight 0 at idx 8 anyway)
    total_no_cd = 0
    for i, (val, w) in enumerate(zip(vals, _VIN_WEIGHTS)):
        if i == 8:
            continue
        if val is None:
            # We can still compute substitutions for this exact index (i),
            # but the global sum will be computed as "excluding i" below.
            continue
        total_no_cd += val * w

    # Handle index 8 (check digit) substitution (unique, derived from others)
    expected_cd = None
    # We can compute expected check digit if all non-8 positions have known values
    # (vals[j] is not None for j != 8)
    if all((vals[j] is not None) for j in range(17) if j != 8):
        remainder = total_no_cd % 11
        expected_cd = "X" if remainder == 10 else str(remainder)

    # Current check digit in candidate
    cd_char = v[8]
    cd_val: Optional[int] = None
    if cd_char == "X":
        cd_val = 10
    elif cd_char.isdigit():
        cd_val = int(cd_char)
    else:
        cd_val = None  # invalid check-digit char

    # If we know the expected CD and the current differs, we can propose it.
    if expected_cd is not None and v[8] != expected_cd:
        subs[8] = {expected_cd}

    # If the current check-digit char is invalid, we cannot solve for other indices
    # under the "exactly one substitution" assumption (the only valid correction is at 8).
    if cd_val is None:
        return subs

    # For each non-8 index, compute required transliteration value so that checksum matches cd_val.
    for i, (val_i, w_i) in enumerate(zip(vals, _VIN_WEIGHTS)):
        if i == 8:
            continue
        inv = _INV_WEIGHT_BY_INDEX[i]
        if inv is None:
            continue  # should not happen except i == 8

        # Sum excluding index i and check-digit contribution
        s_excl_i = total_no_cd - ((val_i * w_i) if val_i is not None else 0)

        # Solve for required value at i: (s_excl_i + val_req * w_i) % 11 == cd_val
        # => val_req = (cd_val - s_excl_i) * inv(w_i) (mod 11)
        val_req = ((cd_val - s_excl_i % 11) * inv) % 11

        # Only 0..9 are valid transliteration values for regular positions (not 10)
        if val_req == 10:
            continue

        # Allowed replacement characters for this numeric value
        allowed = _VIN_VALUE_TO_CHARS.get(val_req, set())
        if not allowed:
            continue

        orig_char = v[i]
        possible = {c for c in allowed if c != orig_char}
        if possible:
            subs[i] = possible

    return subs


def is_one_char_checksum_substitution_match(
    candidate: str, target: str
) -> Tuple[bool, Optional[int]]:
    """
    Returns (True, index) if target == candidate with exactly one character replaced,
    and that replacement is in the checksum-derived substitution set for candidate.
    Returns (False, None) otherwise. Also returns True if candidate == target and target is a valid VIN.
    """
    c = _vin_clean(candidate)
    t = _vin_clean(target)
    if len(c) != 17 or len(t) != 17:
        return (False, None)

    if c == t:
        return (is_valid_vin(t), None)

    diffs = [i for i, (a, b) in enumerate(zip(c, t)) if a != b]
    if len(diffs) != 1:
        return (False, None)

    i = diffs[0]
    subs = possible_checksum_substitutions(c)
    allowed = subs.get(i, set())
    return (t[i] in allowed and c[:i] + t[i] + c[i + 1 :] == t and is_valid_vin(t), i)
