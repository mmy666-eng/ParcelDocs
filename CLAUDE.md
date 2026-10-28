# CLAUDE.md

הנחיות ל-Claude Code בעבודה על ParcelDocs.

ParcelDocs: פוליליין סגור ב-AutoCAD/ZWCAD (ITM, EPSG:2039) ← זיהוי גוש/חלקה ← הורדת היתרי בנייה ותב"עות מאושרות של בני ברק לתיקייה מקומית מסודרת.

## כללי עבודה
- המסמך המחייב הוא `PRD.md`. לא לחרוג ממנו בלי לשאול.
- לעבוד שלב אחד בכל פעם לפי PRD §8. לא לקפוץ קדימה.
- לעבור בין שלבי סעיף 8 ב-PRD ברצף, בתנאי שכל הבדיקות בשלב הנוכחי עוברות.
- בסוף כל שלב לעדכן את `docs/progress.md`: מה נעשה, מה נבדק, מה פתוח.
- לעצור ולשאול רק אם: מקור חסום (התחברות/CAPTCHA/תשלום/חסימת IP), צריך החלטה שלא כתובה ב-PRD, או בדיקות נכשלות 3 פעמים ברצף.
- אם מקור חסום: לסמן אותו "דורש פעולה ידנית" ב-`docs/sources.md` ולהמשיך לחלקים שלא תלויים בו.

## מבנה
- `service/` — שירות Python 3.12 (FastAPI, פורט 8765) + CLI. המבנה הפנימי מוגדר ב-PRD §5.1.
- `plugin/` — תוסף C# .NET. פרויקט אחד, שני Build Configurations: `AutoCAD` ו-`ZWCAD`.
- `docs/sources.md` — נקודות הקצה המאומתות של כל המקורות.
- `output/` — פלט שנוצר אוטומטית. לא לערוך ידנית ולא לעשות לו commit.

## מקורות ורשת
- **אסור להמציא כתובות URL או נקודות קצה.** כל מקור צריך להיות מאומת ומתועד ב-`docs/sources.md`.
- **לא לעקוף CAPTCHA, התחברות או תשלום.** פריט כזה מסומן "דורש פעולה ידנית".
- Rate limit: עד בקשה אחת בשנייה לכל מקור, עם User-Agent מזהה.
- כל מקור נתונים מומש כ-Provider (`PlansProvider` / `PermitsProvider`, PRD §5.2). אין לוגיקה ספציפית לעירייה מחוץ ל-`providers/`.
- כל Provider חדש מקבל בדיקות ב-`tests/` עם תגובות שמורות (fixtures). בזמן בדיקות אין פנייה לרשת.

## תחום
- קואורדינטות: תמיד ITM (EPSG:2039) בתוך המערכת. באפר שלילי של 0.5 מ' לפני חיתוך מול הקדסטר.
- תב"עות: מאושרות בלבד (סטטוס "מתן תוקף").
- שמות קבצים ותיקיות בעברית עוברים תמיד דרך `utils/filenames.py`.
- הרצה חוזרת לא מורידה שוב קבצים קיימים (cache + SHA256 ב-manifest.json).

## סגנון קוד
- Python: type hints, pydantic למודלים, ruff לעיצוב קוד.
- C#: לשמור על תאימות AutoCAD ו-ZWCAD מאותו קוד. ההבדלים רק דרך `#if ZWCAD` או קובץ alias.

## שלב נוכחי
שלב 0 (מחקר מקורות) הושלם, ראו `docs/sources.md`. שלב 1 (תב"עות ב-CLI) אושר ובביצוע.

## פקודות
מתוך `service/` (Python 3.12, venv ב-`service/.venv`):
- התקנה: `py -3.12 -m venv .venv` ואז `.venv\Scripts\python -m pip install -e ".[dev]"`
- בדיקות: `.venv\Scripts\python -m pytest -q` (בלי רשת, fixtures ב-`tests/fixtures/`)
- עיצוב ובדיקת קוד: `.venv\Scripts\python -m ruff format app tests` ו-`.venv\Scripts\python -m ruff check app tests`
- הרצה: `.venv\Scripts\python -m app.cli --gush 6123 --helka 443 --project "שם" [--dry-run]`
