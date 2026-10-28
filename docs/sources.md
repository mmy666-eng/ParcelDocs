# מקורות נתונים – שלב 0

נבדק ב-2026-10-28 בקריאות HTTP אמיתיות (curl, User-Agent מזהה, בקשה אחת בשנייה לכל היותר).
חלקת מבחן: גוש 6123 חלקה 443 בבני ברק (נקודה ITM ‎183620,665826).

## סיכום

| צורך | מקור | סטטוס | גישה |
|---|---|---|---|
| גוש/חלקה מפוליגון | GovMap open WFS (מפ"י) | ✅ עובד | WFS 2.0, GeoJSON, ITM |
| תב"עות – חיתוך מרחבי | iplan ArcGIS `Xplan/MapServer/1` | ✅ עובד | ArcGIS REST query, ITM |
| תב"עות – רשימה לפי חלקה (כולל ישנות) | Complot בני ברק `GetGushFile` | ✅ עובד | HTML |
| תב"עות – מסמכים | Complot ← `archive.gis-net.co.il` | ⚠️ עובד, robots.txt אוסר | PDF ישיר |
| תב"עות – מסמכים ממבא"ת | `mavat.iplan.gov.il/rest/api` | ⛔ reCAPTCHA | לא בשימוש |
| כתובות | Complot `GetGushFile` (סעיף כתובות) | ✅ עובד | HTML |
| היתרים | Complot `GetBakashotByGush` / `GetBakashotByAddress` | ✅ עובד | HTML |
| היתרים – מסמכים | Complot ← `archive.gis-net.co.il` | ⚠️ עובד, robots.txt אוסר | PDF ישיר |
| רישוי זמין | לא נמצא ממשק ציבורי נפרד | ❓ פתוח | – |

---

## 1. קדסטר – GovMap open WFS (מפ"י)

- Base: `https://open.govmap.gov.il/geoserver/opendata/wfs`
- Capabilities: `?service=WFS&request=GetCapabilities`
- שכבות רלוונטיות: `opendata:Parcels_ITM` (חלקות, ITM), `opendata:SUB_GUSH_ALL_ITM` (גושים, ITM), `opendata:muni_il` (רשויות).
- שדות עיקריים ב-`Parcels_ITM`: `GUSH_NUM`, `GUSH_SUFFI`, `PARCEL`, `LEGAL_AREA`, `STATUS_TEX`, `LOCALITY_N`, `LOCALITY_I` (בני ברק = 6100), `ID` (למשל `6123--443`).
- שאילתה שנבדקה:
  ```
  ?service=WFS&version=2.0.0&request=GetFeature&typeNames=opendata:Parcels_ITM
   &outputFormat=application/json&srsName=EPSG:2039
   &bbox=183600,665800,183700,665900,EPSG:2039&count=2
  ```
  התוצאה: גוש 6123 חלקה 443, "מוסדר", בני ברק.
- מימוש: שאילתת bbox של הפוליגון, ואחר כך חיתוך מדויק ב-shapely עם באפר שלילי של 0.5 מ'. עוד לא נבדק אם השרת תומך בפילטר `INTERSECTS` (CQL/FES), שהיה חוסך את הסינון המקומי.
- `www.govmap.gov.il` עצמו מחזיר 403 לבקשות שאינן מדפדפן. לא צריך אותו.

> ⚠️ **אי-התאמה בקדסטר:** לפי מפ"י, 6123/443 היא חלקה קיימת. אצל העירייה (Complot) היא "עברה פרצלציה" לחלקות 2518 ו-2519. צריך לבדוק את `GetGushFile` על כל חלקה, ואם מתקבלת הפניה לחלקות בנות, להמשיך אליהן.

## 2. תב"עות – iplan ArcGIS

- Base: `https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer`
- שכבה **1** "קוים כחולים – תכניות מקוונות" (פוליגונים, wkid 2039, maxRecordCount=1000, JSON/geoJSON).
- שאילתה שנבדקה:
  ```
  /1/query?geometry=183620,665826&geometryType=esriGeometryPoint&inSR=2039
   &spatialRel=esriSpatialRelIntersects&returnGeometry=false&f=json
   &outFields=pl_number,pl_name,station_desc,internet_short_status,entity_subtype_desc,pl_date_8,pl_url
  ```
  לפוליגון אפשר להעביר `geometryType=esriGeometryPolygon` עם `geometry={"rings":[...]}`.
- שדות: `pl_number`, `pl_name`, `station_desc` (מאושרת = `"אישור"`), `entity_subtype_desc` (מקומית/מתאר/כוללנית/ארצית), `pl_date_8` (תאריך פרסום ברשומות, epoch ms), `pl_url` (קישור למבא"ת), `mp_id`.
- התוצאה בחלקת המבחן: 11 תכניות, מתוכן 7 בסטטוס "אישור" (כולל `תמא/ 60` ו-`501-0308296` בב/2035).
- **מגבלה:** השכבה כוללת רק תכניות "מקוונות". תכניות ישנות (למשל `105/ד/2` משנת 2001) מופיעות ב-Complot ולא כאן. לכן צריך לאחד את שני המקורות.

## 3. מבא"ת – mavat.iplan.gov.il ⛔

- ה-API של האתר: `https://mavat.iplan.gov.il/rest/api`. נמצא בקוד ה-JS של האתר.
- בקוד האתר: `IsReCaptcha: true`. כל קריאה, חוץ מרשימה קצרה (`getAboutHtml`, `GetAppealUrl`, `Luts/...`), דורשת טוקן reCAPTCHA v3.
- `robots.txt` אוסר `*.pdf`.
- **מסקנה לפי PRD §7:** לא משתמשים ב-API הזה ולא מורידים ממנו. בדוח שומרים את `pl_url` כקישור ידני. מסמכים של תב"עות לוקחים מ-Complot (סעיף 4).

## 4. Complot – מערכת המידע ההנדסי של בני ברק

העירייה מטמיעה את Complot בעמוד `https://www.bnei-brak.muni.il/תושבים/vadad-tichnun-bnya/request/` עם `site_id = 75`.

- Base: `https://handasi.complot.co.il/magicscripts/mgrqispi.dll?appname=cixpa&prgname=`
- כל התשובות הן קטעי **HTML** (לא JSON). צריך לפרסר אותם (למשל BeautifulSoup).

| פעולה | פרמטרים | מה מחזיר |
|---|---|---|
| `GetGushFile` | `&siteid=75&g=<gush>&h=<helka>&arguments=siteid,g,h` | פרטי חלקה, **כתובות**, **תכניות** (מספר, שם, סטטוס "בתוקף", תאריך, מזהה פנימי `getTaba(n)`), או הפניה לחלקות בנות אחרי פרצלציה |
| `GetUnifiedFile` | `&siteid=75&g=&h=&arguments=siteid,g,h` | תיק מידע מאוחד: תכניות, תיקי בניין ובקשות בתחום |
| `GetTabaFile` | `&siteid=75&n=<numerator>&arguments=siteid,n` | פרטי תכנית, שלבים (כולל מספר ילקוט פרסומים) וקישורי PDF |
| `GetBakashotByGush` | `&siteid=75&grp=0&t=0&g=<gush>&h=<helka>&l=true&arguments=siteId,grp,t,g,h,l` | רשימת בקשות: מספר בקשה, תיק בניין, תאריך, כתובת, גוש/חלקה, ארכיב מסמכים. ברירת המחדל מחזירה 50 תוצאות |
| `GetBakashotByAddress` | `&siteid=75&grp=0&t=0&c=<city>&s=<street>&h=<house>&l=true&arguments=siteId,grp,t,c,s,h,l` | כנ"ל, לפי כתובת. קודי `c` ו-`s` עוד לא נבדקו (`GetStreets` ב-asmx) |
| `GetBakashotByHeter` | `&siteid=75&grp=0&t=0&b=<heter>&l=true&arguments=siteId,grp,t,b,l` | לפי מספר היתר |
| `GetBakashaFile` | `&siteid=75&b=<bakasha>&arguments=siteid,b` | פרטי בקשה: **מספר היתר**, **תאריך הפקת היתר**, תיאור, סוג, שימוש, בעלי עניין, גוש/חלקה, אירועים, החלטות ועדה, קישור PDF |
| `GetTikFile` | `&siteid=75&t=<tik>&arguments=siteid,t` | תיק בניין |

- Web service נלווה: `https://handasi.complot.co.il/wsComplotPublicData/ComplotPublicData.asmx` (פעולות: `GetStreets`, `GetHelkot`, `GetGushim`, `GetTaba`, `GetBakashot`, `GetHeterNumbers` ועוד). הפרמטרים שלו עוד לא נבדקו.
- בדיקות שעשיתי:
  - `GetGushFile` 6123/2518 החזיר כתובת "הבנים 3" ו-14 תכניות בתוקף, כולל תכניות ישנות.
  - `GetBakashotByGush` על גוש 6123 (כל החלקות) החזיר 1,242 בקשות.
  - `GetBakashaFile` 20177 החזיר היתר 9545 מתאריך 23/03/1989 עם קישור PDF.
- לכותרת העמודה "מספר בקשה" מצורף "(רישוי זמין)". כנראה ש-Complot מציג גם בקשות של רישוי זמין.
- לא נמצא CAPTCHA. בכתובת `robots.txt` השרת מחזיר עמוד שגיאה, כלומר אין לו קובץ robots.

## 5. ארכיון מסמכים – archive.gis-net.co.il ⚠️

- קבצי PDF ישירים, למשל `https://archive.gis-net.co.il/bneybrak/Pirsumim/20/<guid>.pdf`. החזיר HTTP 200 עם `application/pdf`.
- הקישורים מופיעים בעמודי `GetTabaFile` ו-`GetBakashaFile`.
- **`robots.txt`: `User-agent: * / Disallow: /`**. ההגבלה הזו מיועדת לזחלנים. הכלי שלנו מוריד רק קבצים ספציפיים שמשתמש ביקש, ולא סורק את האתר. **זו החלטה שלכם.** ראו "שאלות פתוחות".

## 6. מפת GIS עירונית – gis-net

- `https://v5.gis-net.co.il/v5/BneiBraq`, ומזהה הלקוח בממשק שלה הוא `Customer=6100`.
- קישור התמקדות מתוך Complot: `https://v5.gis-net.co.il/v5api/Home/Focus?Customer=6100&FocusType=3&Where=<gush>,<helka>&AnswerType=1`.
- לא נבדק לעומק. זה מקור אפשרי לשכבת כתובות ולשכבת תכניות של העירייה.

## 7. רישוי זמין ❓

- לא נמצא ממשק חיפוש ציבורי ארצי. לפי תשובה של מינהל התכנון, המידע מונגש דרך אתרי ההנדסה של הרשויות.
- בבני ברק Complot כבר מציג מספרי בקשה של רישוי זמין. כנראה שבגרסה 1 לא צריך Provider נפרד.

---

## שאלות פתוחות

1. **robots.txt של archive.gis-net.co.il** אוסר הכל. האם להוריד ממנו קבצים ספציפיים לפי בקשת משתמש, עם קצב איטי ו-User-Agent מזהה? אם לא, כל המסמכים של בני ברק יסומנו "דורש פעולה ידנית" עם קישור.
2. **מבא"ת חסום ב-reCAPTCHA.** מסמכים של תכניות ארציות ומחוזיות שאין להן עותק ב-Complot יסומנו "דורש פעולה ידנית" עם `pl_url`.
3. **אי-התאמת פרצלציה** בין מפ"י לעירייה: צריך לממש מעקב אחרי ההפניה לחלקות בנות.
4. קודי רחוב ועיר לחיפוש לפי כתובת: לבדוק את `GetStreets` ב-asmx.
5. עימוד ב-`GetBakashotByGush`: ברירת המחדל מחזירה 50 תוצאות. צריך לבדוק את `getUnlimitedSearch` או חיפוש לפי חלקה בודדת.
