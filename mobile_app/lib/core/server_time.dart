/// Server timestamps.
///
/// The edge server stores and returns pipeline timestamps (incidents, events,
/// interactions, tracks, tripwire crossings, recordings) as naive UTC ISO-8601
/// strings with no offset (edge_backend/app/services/timeutil.py). Dart's
/// `DateTime.parse` would read those as *local* time and shift every time by
/// the phone's offset, so an offset-less value is treated as UTC here.
/// Values that carry `Z` or `+hh:mm` / `-hhmm` keep their offset.
///
/// Returns local time for display, or null for null/empty/unparseable input.
DateTime? parseServerTime(dynamic value) {
  if (value == null) return null;
  final s = value.toString().trim();
  if (s.isEmpty) return null;
  final String iso;
  if (_hasOffset(s)) {
    iso = s;
  } else if (RegExp(r'^\d{4}-\d{2}-\d{2}$').hasMatch(s)) {
    iso = '${s}T00:00:00Z'; // date only: UTC midnight
  } else {
    iso = '${s}Z';
  }
  final parsed = DateTime.tryParse(iso);
  return parsed?.toLocal();
}

/// Offset information only counts after the time part (a date-only value
/// such as `2026-09-23` contains dashes but no offset).
bool _hasOffset(String s) {
  final t = s.indexOf(RegExp('[Tt ]'));
  if (t < 0) return s.endsWith('Z') || s.endsWith('z');
  final time = s.substring(t + 1);
  return time.endsWith('Z') || time.endsWith('z') || RegExp(r'[+-]\d{2}(:?\d{2})?$').hasMatch(time);
}
