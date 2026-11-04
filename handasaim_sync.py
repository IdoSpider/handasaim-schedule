#!/usr/bin/env python3
"""
Handasaim School Schedule to Apple Calendar Sync
Fetches daily schedule from https://handasaim.co.il/schedule (Firebase/Firestore)
and syncs it directly into Apple Calendar on macOS.
"""

import os
import sys
import json
import platform
import argparse
import subprocess
from datetime import datetime, date, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path

FIRESTORE_PROJECT_ID = "handasaim-website"
FIRESTORE_API_KEY = "AIzaSyAcH6kj2INKBW0-YyXu9mcMo6DCCbamaiw"
DEFAULT_CLASS = "יא 1"
DEFAULT_CALENDAR = "School Schedule"

# Standard bell times for Handasaim in case a period is missing times
STANDARD_BELL_TIMES = {
    0: ("07:45", "08:30"),
    1: ("08:30", "09:15"),
    2: ("09:15", "10:00"),
    3: ("10:15", "11:00"),
    4: ("11:00", "11:45"),
    5: ("12:10", "12:55"),
    6: ("12:55", "13:40"),
    7: ("13:50", "14:35"),
    8: ("14:35", "15:20"),
    9: ("15:25", "16:10"),
    10: ("16:10", "16:55"),
    11: ("16:55", "17:40"),
}


def decode_firestore_value(val):
    """Recursively converts Firestore REST API typed values to standard Python values."""
    if not isinstance(val, dict):
        return val
    if "stringValue" in val:
        return val["stringValue"]
    if "integerValue" in val:
        return int(val["integerValue"])
    if "doubleValue" in val:
        return float(val["doubleValue"])
    if "booleanValue" in val:
        return val["booleanValue"]
    if "timestampValue" in val:
        return val["timestampValue"]
    if "nullValue" in val:
        return None
    if "arrayValue" in val:
        return [decode_firestore_value(v) for v in val["arrayValue"].get("values", [])]
    if "mapValue" in val:
        return {k: decode_firestore_value(v) for k, v in val["mapValue"].get("fields", {}).items()}
    return val


def fetch_firestore_doc(collection, doc_id):
    """Fetches a document from Firestore REST API."""
    url = f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}/databases/(default)/documents/{collection}/{doc_id}?key={FIRESTORE_API_KEY}"
    req = Request(url, headers={"User-Agent": "HandasaimCalSync/1.0"})
    try:
        with urlopen(req) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            fields = raw.get("fields", {})
            return {k: decode_firestore_value(v) for k, v in fields.items()}
    except HTTPError as e:
        if e.code == 404:
            return None
        raise
    except URLError as e:
        print(f"Network error connecting to school API: {e}", file=sys.stderr)
        return None


def normalize_class_name(name):
    """Normalizes class names for consistent matching (e.g. 'יא1' -> 'יא 1')."""
    if not name:
        return ""
    clean = name.strip().replace(" ", "")
    # Add space between Hebrew grade prefix and class number
    for prefix in ["ז", "ח", "ט", "י", "יא", "יב"]:
        if clean.startswith(prefix) and clean[len(prefix):].isdigit():
            return f"{prefix} {clean[len(prefix):]}"
    return name.strip()


def get_teacher_first_name(teacher_full):
    """Extracts first name from teacher string (in Handasaim db format is 'משפחה פרטי')."""
    if not teacher_full:
        return ""
    parts = teacher_full.strip().split()
    return parts[-1] if parts else ""


def teacher_matches(preferred_name, actual_name):
    """Checks if actual teacher name matches preferred name (handles first/last name order)."""
    if not preferred_name or not actual_name:
        return False
    words_pref = set(preferred_name.strip().split())
    words_act = set(actual_name.strip().split())
    return words_pref.issubset(words_act)


def parse_day_schedule(doc_data, target_class_normalized, user_preferences=None):
    """
    Parses a schedule document for the specified class.
    Returns a list of structured events.
    """
    if not doc_data:
        return []

    date_str = doc_data.get("date")
    day_name = doc_data.get("day_name", "")
    lessons = doc_data.get("lessons", [])
    events = []

    preferred_teachers = (user_preferences or {}).get("teachers", {})
    excluded_subjects = (user_preferences or {}).get("excluded_subjects", [])

    for lesson in lessons:
        lesson_num = lesson.get("lesson_num", 0)
        time_start = lesson.get("time_start")
        time_end = lesson.get("time_end")

        # Fallback to standard bell times if not provided
        if not time_start or not time_end:
            if lesson_num in STANDARD_BELL_TIMES:
                time_start, time_end = STANDARD_BELL_TIMES[lesson_num]
            else:
                continue

        classes_map = lesson.get("classes", {})
        # Find matching class key
        matched_key = None
        for k in classes_map:
            if normalize_class_name(k) == target_class_normalized:
                matched_key = k
                break

        if not matched_key:
            continue

        raw_items = classes_map[matched_key]
        if not raw_items:
            continue

        # Check for changes / colors / labels
        cell_colors = lesson.get("cell_colors", {})
        cell_labels = lesson.get("cell_labels", {})
        has_color_change = bool(cell_colors.get(matched_key))
        label_note = cell_labels.get(matched_key, "")

        # 1. Filter out excluded subjects (e.g. computer science, cyber, algorithmics if not taken)
        items_after_exclusion = []
        for item in raw_items:
            subj = item.get("subject", "").strip()
            if any(exc in subj for exc in excluded_subjects):
                continue
            items_after_exclusion.append(item)

        if not items_after_exclusion:
            # All options in this period were excluded (e.g. student does not take any of the parallel tracks)
            continue

        # 2. Filter by preferred teachers
        selected_items = []
        if preferred_teachers:
            for item in items_after_exclusion:
                subj = item.get("subject", "").strip()
                teacher = item.get("teacher", "").strip()

                # Check if this subject has a specific preferred teacher configured
                subject_has_pref = False
                for pref_subj, pref_teacher in preferred_teachers.items():
                    if pref_subj in subj:
                        subject_has_pref = True
                        if teacher_matches(pref_teacher, teacher):
                            selected_items.append(item)
                        break

                if not subject_has_pref:
                    # Subject is not constrained by preferences (e.g. general subjects or electives without preference)
                    selected_items.append(item)
        else:
            selected_items = items_after_exclusion

        if not selected_items:
            # Parallel tracks exist, but user's group doesn't have a class right now
            continue

        # Build title and description
        if len(selected_items) == 1:
            it = selected_items[0]
            subj = it.get("subject", "שיעור")
            teacher = it.get("teacher", "")
            first_name = get_teacher_first_name(teacher)
            title = f"{lesson_num}: {subj}" + (f" - {first_name}" if first_name else "")
            description = f"שיעור {lesson_num} | {day_name}\nמקצוע: {subj}\nמורה: {teacher}"
        else:
            # Multiple groups / tracks
            subj_list = []
            desc_lines = [f"שיעור {lesson_num} | {day_name} (קבוצות / מגמות):"]
            for it in selected_items:
                s = it.get("subject", "")
                t = it.get("teacher", "")
                fn = get_teacher_first_name(t)
                subj_list.append(f"{s}" + (f" - {fn}" if fn else ""))
                desc_lines.append(f"• {s} — {t}")
            title = f"{lesson_num}: " + " / ".join(subj_list[:2])
            if len(subj_list) > 2:
                title += f" (+{len(subj_list)-2})"
            description = "\n".join(desc_lines)

        if has_color_change or label_note:
            note_str = f" [שינוי במערכת{(': ' + label_note) if label_note else ''}]"
            title = "⚠️ " + title + note_str
            description += f"\n\nשים לב: חל שינוי בשיעור זה! {label_note}"

        events.append({
            "lesson_num": lesson_num,
            "title": title,
            "start": f"{date_str} {time_start}",
            "end": f"{date_str} {time_end}",
            "location": "הנדסאים הרצליה",
            "description": description
        })

    return events


def sync_events_to_apple_calendar(calendar_name, events, target_date_str):
    """
    Uses AppleScript via osascript to:
    1. Ensure the calendar exists (creates it if missing).
    2. Remove any previous events for target_date_str in that calendar.
    3. Insert the newly fetched events.
    """
    y, m, d = [int(x) for x in target_date_str.split("-")]

    script_lines = [
        'tell application "Calendar"',
        f'    if not (exists calendar "{calendar_name}") then',
        f'        make new calendar with properties {{name:"{calendar_name}"}}',
        '    end if',
        f'    set theCal to calendar "{calendar_name}"',
        f'    set dayStart to current date',
        f'    set year of dayStart to {y}',
        f'    set month of dayStart to {m}',
        f'    set day of dayStart to {d}',
        f'    set hours of dayStart to 0',
        f'    set minutes of dayStart to 0',
        f'    set seconds of dayStart to 0',
        f'    set dayEnd to dayStart + (24 * 60 * 60)',
        '    tell theCal',
        '        set oldEvents to (every event whose start date >= dayStart and start date < dayEnd)',
        '        repeat with oldEv in oldEvents',
        '            delete oldEv',
        '        end repeat',
    ]

    for ev in events:
        sy, sm, sd = [int(x) for x in ev["start"].split()[0].split("-")]
        sh, smin = [int(x) for x in ev["start"].split()[1].split(":")]
        ey, em, ed = [int(x) for x in ev["end"].split()[0].split("-")]
        eh, emin = [int(x) for x in ev["end"].split()[1].split(":")]

        # Escape strings for AppleScript
        title = ev["title"].replace("\\", "\\\\").replace('"', '\\"')
        desc = ev.get("description", "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        loc = ev.get("location", "").replace("\\", "\\\\").replace('"', '\\"')

        script_lines.extend([
            '        set sDate to current date',
            f'        set year of sDate to {sy}',
            f'        set month of sDate to {sm}',
            f'        set day of sDate to {sd}',
            f'        set hours of sDate to {sh}',
            f'        set minutes of sDate to {smin}',
            f'        set seconds of sDate to 0',
            '        set eDate to current date',
            f'        set year of eDate to {ey}',
            f'        set month of eDate to {em}',
            f'        set day of eDate to {ed}',
            f'        set hours of eDate to {eh}',
            f'        set minutes of eDate to {emin}',
            f'        set seconds of eDate to 0',
            f'        make new event with properties {{summary:"{title}", start date:sDate, end date:eDate, location:"{loc}", description:"{desc}"}}'
        ])

    script_lines.extend([
        '    end tell',
        'end tell'
    ])

    full_script = "\n".join(script_lines)
    proc = subprocess.run(["osascript", "-e", full_script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"AppleScript error: {proc.stderr.strip()}")


def export_ics(events, filepath, cal_name="School Schedule"):
    """Exports events to an iCalendar (.ics) file."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Handasaim Schedule Sync//HE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{cal_name}",
        "X-WR-TIMEZONE:Asia/Jerusalem",
    ]

    for ev in events:
        s_dt = datetime.strptime(ev["start"], "%Y-%m-%d %H:%M")
        e_dt = datetime.strptime(ev["end"], "%Y-%m-%d %H:%M")
        uid = f"{s_dt.strftime('%Y%m%dT%H%M%S')}-p{ev.get('lesson_num', 0)}@handasaim"
        clean_desc = ev["description"].replace("\n", "\\n")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;TZID=Asia/Jerusalem:{s_dt.strftime('%Y%m%dT%H%M00')}",
            f"DTEND;TZID=Asia/Jerusalem:{e_dt.strftime('%Y%m%dT%H%M00')}",
            f"SUMMARY:{ev['title']}",
            f"DESCRIPTION:{clean_desc}",
            f"LOCATION:{ev.get('location', '')}",
            "STATUS:CONFIRMED",
            "END:VEVENT"
        ])

    lines.append("END:VCALENDAR")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines))


def get_current_school_week_dates(ref_date=None):
    """Returns list of YYYY-MM-DD for Sunday through Friday of the given week."""
    if ref_date is None:
        ref_date = date.today()
    # Sunday is start of week in Israel (weekday() is 6 for Sun in Python, or 0 for Mon)
    # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    day_idx = (ref_date.weekday() + 1) % 7 # Sun=0, Mon=1, ..., Sat=6
    if day_idx == 6: # Saturday -> jump to next Sunday
        sun = ref_date + timedelta(days=1)
    else:
        sun = ref_date - timedelta(days=day_idx)
    return [(sun + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6)]


def install_daily_agent(script_path, hour=19, minute=0):
    """Sets up a macOS launchd agent to run automatically every evening at 19:00 to sync tomorrow."""
    agent_dir = Path.home() / "Library" / "LaunchAgents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agent_dir / "com.handasaim.schedule.plist"
    
    python_bin = sys.executable
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.handasaim.schedule</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>{os.path.abspath(script_path)}</string>
        <string>--date</string>
        <string>tomorrow</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{os.path.expanduser('~/.handasaim_sync.log')}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.expanduser('~/.handasaim_sync.log')}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    res = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if res.returncode == 0:
        print(f"✅ Successfully installed LaunchAgent: {plist_path}")
        print(f"   The schedule will automatically sync every evening at {hour:02d}:{minute:02d} for tomorrow's schedule!")
    else:
        print(f"⚠️ Installed plist, but launchctl reported: {res.stderr.strip()}")


def main():
    parser = argparse.ArgumentParser(description="Sync Handasaim school schedule to Apple Calendar.")
    parser.add_argument("--class-name", default=DEFAULT_CLASS, help=f"Class name (default: {DEFAULT_CLASS})")
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR, help=f"Apple Calendar name (default: {DEFAULT_CALENDAR})")
    parser.add_argument("--date", default="today", help="Date to sync: 'today', 'tomorrow', or 'YYYY-MM-DD'")
    parser.add_argument("--week", action="store_true", help="Sync all available days for the current week")
    parser.add_argument("--export-ics", metavar="FILE", help="Also export events to an .ics file")
    parser.add_argument("--only-ics", action="store_true", help="Only export to .ics without calling Apple Calendar (for Linux/cloud)")
    parser.add_argument("--install-agent", action="store_true", help="Install macOS launchd agent for automatic daily sync at 07:00")
    parser.add_argument("--config", help="Path to config.json with teacher preferences")

    args = parser.parse_args()

    if args.install_agent:
        install_daily_agent(__file__)
        return

    # Load preferences if exists
    config_file = args.config or os.path.join(os.path.dirname(__file__), "config.json")
    user_preferences = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                user_preferences = json.load(f)
                print(f"Loaded configuration from {config_file}")
        except Exception as e:
            print(f"Warning: could not load {config_file}: {e}")

    class_name = args.class_name or user_preferences.get("class_name") or DEFAULT_CLASS
    calendar_name = args.calendar or user_preferences.get("calendar_name") or DEFAULT_CALENDAR

    target_class_norm = normalize_class_name(class_name)

    # Determine dates to sync
    today_dt = date.today()
    if args.week:
        dates_to_sync = get_current_school_week_dates(today_dt)
    elif args.date == "today":
        dates_to_sync = [today_dt.strftime("%Y-%m-%d")]
    elif args.date == "tomorrow":
        dates_to_sync = [(today_dt + timedelta(days=1)).strftime("%Y-%m-%d")]
    elif args.date == "both" or args.date == "auto":
        dates_to_sync = [
            today_dt.strftime("%Y-%m-%d"),
            (today_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        ]
    else:
        dates_to_sync = [args.date]

    all_synced_events = []

    print(f"📅 Syncing schedule for class '{target_class_norm}' to calendar '{calendar_name}'...")

    for d in dates_to_sync:
        doc = fetch_firestore_doc("schedules", d)
        if not doc:
            print(f"ℹ️  {d}: No schedule published yet on the website.")
            continue

        day_name = doc.get("day_name", "")
        events = parse_day_schedule(doc, target_class_norm, user_preferences)
        print(f"\n🗓️  {d} ({day_name}): Found {len(events)} lessons for {target_class_norm}")
        for ev in events:
            time_part = ev["start"].split()[1] + "-" + ev["end"].split()[1]
            print(f"   • {time_part} | {ev['title']}")

        if events:
            all_synced_events.extend(events)
            if platform.system() == "Darwin" and not getattr(args, "only_ics", False):
                try:
                    sync_events_to_apple_calendar(calendar_name, events, d)
                    print(f"   ✅ Synced {len(events)} events to Apple Calendar '{calendar_name}'")
                except Exception as e:
                    print(f"   ❌ Failed to sync to Apple Calendar: {e}", file=sys.stderr)

    if args.export_ics:
        if all_synced_events:
            export_ics(all_synced_events, args.export_ics, cal_name=calendar_name)
            print(f"\n📁 Exported {len(all_synced_events)} events to {args.export_ics}")
        else:
            print("\n⚠️ No events to export.")


if __name__ == "__main__":
    main()
