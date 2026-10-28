# מקורות נתונים – שלב 0

נבדק ב-2026-10-28 בקריאות HTTP אמיתיות (curl, User-Agent מזהה, בקשה אחת בשנייה לכל היותר).

**חלקת מבחן:** גוש 6123 חלקה 2518, הבנים 3, בני ברק. לפי מפ"י זו עדיין חלקה 443 (ראו §1.3). נקודה ב-ITM: ‎183620,665826.

**רמות ביטחון:** ✅ מאומת = נבדק בקריאה אמיתית והחזיר נתונים נכונים · 🟡 חלקי = עובד, אבל חסר אימות של פרט מהותי · ❌ לא נמצא / חסום.

## סיכום

| # | צורך | מקור | סוג | מגבלות | ביטחון |
|---|---|---|---|---|---|
| 1 | חלקות לפי פוליגון | GovMap open WFS (מפ"י) | OGC WFS 2.0 → GeoJSON | אין. לא מעודכן אחרי פרצלציות | ✅ |
| 2 | כתובות | Complot בני ברק (`GetGushFile`, `GetStreets`) | דף HTML + SOAP | אין התחברות | ✅ |
| 3א | גבולות תב"ע + סינון מאושרות | iplan `Xplan/MapServer/1` | ArcGIS REST | רק תכניות "מקוונות" | ✅ |
| 3ב | תב"עות לפי חלקה (כולל ישנות) | Complot `GetGushFile` | דף HTML | אין התחברות | ✅ |
| 3ג | מסמכי תכנית (תקנון/תשריט) | Complot `GetTabaFile` ← `archive.gis-net.co.il` | HTML + PDF ישיר | robots.txt: `Disallow: /` | ✅ טכנית / ⚠️ החלטה |
| 3ד | מסמכי תכנית ממבא"ת | `mavat.iplan.gov.il/rest/api` | REST (JSON) | **reCAPTCHA v3** | ❌ חסום |
| 4 | רישוי זמין | – | – | דורש הזדהות. אין צפייה ציבורית | ❌ לא נמצא |
| 5 | היתרים (בני ברק) | Complot `GetBakashotBy*`, `GetBakashaFile`, `GetBakashaDocs` | דף HTML | אין התחברות | ✅ |

אף מקור לא דורש דפדפן (Playwright). כולם עובדים ב-HTTP פשוט.

---

## 1. גושים/חלקות – GovMap open WFS (מפ"י) ✅

### 1.1 נקודת קצה
- `https://open.govmap.gov.il/geoserver/opendata/wfs` (GeoServer, OGC WFS 2.0.0)
- סוג: **OGC WFS**, פלט `application/json` (GeoJSON), קואורדינטות `EPSG:2039`.
- שכבות: `opendata:Parcels_ITM` (חלקות), `opendata:SUB_GUSH_ALL_ITM` (גושים), `opendata:muni_il` (רשויות). יש גם גרסאות שאינן ITM, אבל לא משתמשים בהן.
- שדות ב-`Parcels_ITM`: `GUSH_NUM`, `GUSH_SUFFI`, `PARCEL`, `LEGAL_AREA`, `STATUS_TEX`, `LOCALITY_I` (בני ברק = 6100), `LOCALITY_N`, `ID` (`6123--443`), `SYS_DATE`, גאומטריה ב-`the_geom`.

### 1.2 חיפוש לפי פוליגון (מאומת)
```
GET .../wfs?service=WFS&version=2.0.0&request=GetFeature
  &typeNames=opendata:Parcels_ITM&outputFormat=application/json&srsName=EPSG:2039
  &propertyName=GUSH_NUM,PARCEL,LOCALITY_N,STATUS_TEX
  &cql_filter=INTERSECTS(the_geom, SRID=2039;POLYGON((183600 665800,183700 665800,183700 665900,183600 665900,183600 665800)))
```
תגובה: `"numberMatched":29`. בין החלקות: 6123/443, 6123/1012, 6123/764.

הפוליגון נשלח לשרת, והשרת מבצע את החיתוך. באפר שלילי של 0.5 מ' מחושב מקומית ב-shapely לפני השליחה.

חיפוש לפי גוש/חלקה (מאומת): `cql_filter=GUSH_NUM=6123 AND PARCEL=443` החזיר תוצאה אחת, "מוסדר".

### 1.3 מגבלות
- אין התחברות ואין CAPTCHA. `www.govmap.gov.il` (לא open) מחזיר 403 לכלים שאינם דפדפן. לא צריך אותו.
- **לא מעודכן אחרי פרצלציות.** לפי מפ"י 6123/443 קיימת, ו-6123/2518 לא קיימת (`numberMatched:0`). העירייה מציגה את 443 כ"עברה פרצלציה" לחלקות 2518 ו-2519.
- תנאי שימוש: לא מצאתי מסמך רישיון רשמי. השרת נקרא "opendata". API של data.gov.il החזיר 404.

## 2. כתובות ✅

GovMap open WFS לא כולל שכבת כתובות. המקור הוא Complot של העירייה (§5).

| מה | קריאה | תוצאה |
|---|---|---|
| כתובות של חלקה | `GetGushFile` (§5.2), סעיף "כתובות" | 6123/2518 → "הבנים 3, רוחב דרך 10.00" |
| רשימת רחובות וקודים | SOAP `GetStreets` עם `site_id=75` | XML: `<label>הבנים</label><v>423</v><k>6100</k>` |
| קוד יישוב | SOAP `GetYeshuvim` עם `site_id=75` | `בני ברק` = `6100` |

קריאת SOAP:
```
POST https://handasi.complot.co.il/wsComplotPublicData/ComplotPublicData.asmx
Content-Type: text/xml; charset=utf-8
SOAPAction: "https://handasi.complot.co.il/GetStreets"

<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>
  <GetStreets xmlns="https://handasi.complot.co.il"><site_id>75</site_id></GetStreets>
</soap:Body></soap:Envelope>
```
החיפוש ההפוך, מכתובת לגוש/חלקה, נעשה דרך `GetBakashotByAddress` (§5.3), שמחזיר גם את גוש/חלקה של הבקשות.

## 3. תב"עות – מנהל התכנון + עירייה

### 3א. גבולות תכניות – iplan ArcGIS ✅
- `https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer/1` – "קוים כחולים – תכניות מקוונות"
- סוג: **ArcGIS REST** (MapServer 10.81), פוליגונים, wkid 2039, `maxRecordCount=1000`, פלט JSON/geoJSON.

```
GET .../Xplan/MapServer/1/query?geometry=183620,665826&geometryType=esriGeometryPoint
  &inSR=2039&spatialRel=esriSpatialRelIntersects&returnGeometry=false&f=json
  &outFields=pl_number,pl_name,station_desc,internet_short_status,entity_subtype_desc,pl_date_8,pl_url
```
לפוליגון: `geometryType=esriGeometryPolygon&geometry={"rings":[[[x,y],...]],"spatialReference":{"wkid":2039}}`. הצורה הזו לא נבדקה, רק נקודה.

תגובה לדוגמה (11 תכניות בחלקת המבחן):
```json
{"pl_number":"501-0873794","pl_name":"חדרי יציאה לגג ודירות גג בבנייני מגורים",
 "station_desc":"אישור","internet_short_status":"התכנית אושרה",
 "entity_subtype_desc":"תכנית מתאר מקומית","pl_date_8":1674691200000,
 "pl_url":"https://mavat.iplan.gov.il/SV4/1/5005077848/310"}
```

**סינון מאושרות:** `station_desc = 'אישור'` (אפשר בצד השרת: `where=station_desc='אישור'`). ערכים אחרים שנצפו: `"בבדיקה תכנונית"`, `"הכנת הודעה 77/78"`. 7 מתוך 11 מאושרות.

- `entity_subtype_desc` מחזיר את סוג התכנית: מפורטת / מתאר מקומית / כוללנית / מתאר ארצית.
- `pl_date_8` הוא תאריך הפרסום ברשומות (epoch ms).
- **מגבלה:** רק תכניות "מקוונות". תכניות ישנות כמו `105/ד/2` (2001) ו-`מק/105/ה` (2002) לא מופיעות כאן. לכן חובה לאחד עם §3ב.
- תנאי שימוש: לפי gov.il, XPLAN "פתוח לציבור הרחב ללא תשלום" והנתונים "פתוחים לצריכה". לא נמצא רישיון פורמלי.

### 3ב. תב"עות לפי חלקה – Complot ✅
`GetGushFile` (§5.2), סעיף "תוכניות למידע תכנוני". בחלקת המבחן: 14 תכניות, כולן "בתוקף", עם מספר, שם, תאריך ומזהה פנימי `getTaba(<n>)`.

הרשימה כוללת תכניות ישנות, וגם תכניות שמופיעות ב-iplan. מספרי `501-...` מאפשרים לזהות ולאחד כפילויות (לפעמים המספר מופיע בשם התכנית).

### 3ג. מסמכי תכנית – Complot + ארכיון ✅ / ⚠️
```
GET https://handasi.complot.co.il/magicscripts/mgrqispi.dll?appname=cixpa&prgname=GetTabaFile&siteid=75&n=3896&arguments=siteid,n
```
מחזיר HTML עם פרטי התכנית, שלבים (כולל מספר ילקוט פרסומים) וטבלת מסמכים: שם, **נושא** ותאריך, עם קישור PDF. לתכנית 501-0873794 יש 13 מסמכים:

| שם המסמך | נושא | תאריך |
|---|---|---|
| תקנון לתוקף | **תקנון** (= הוראות) | 29/01/2023 |
| מצב מוצע לתוקף | **תשריט** | 29/01/2023 |
| בינוי לתוקף | **תשריט** | 29/01/2023 |
| תקנון לוועדה / מצב מוצע לוועדה | תקנון / תשריט | 10/07/2022 |
| פרסומים, החלטות | צרופות לתב"ע / פרסומי תב"ע | … |

- כלל בחירה מוצע: מסמכים עם המילה "לתוקף" בשם הם הגרסה המאושרת. אם אין כאלה, לוקחים את התאריך האחרון לכל נושא.
- קישור לדוגמה: `https://archive.gis-net.co.il/bneybrak/Pirsumim/76/559ff773-9218-49fe-b73f-03fcb1831904.pdf`. החזיר HTTP 200 עם `application/pdf`.
- **`archive.gis-net.co.il/robots.txt` = `User-agent: * / Disallow: /`**. ראו "שאלות פתוחות".

### 3ד. מבא"ת – mavat.iplan.gov.il ❌
- ה-API של האתר: `https://mavat.iplan.gov.il/rest/api` (מתוך `main.*.js` של האתר).
- בקוד: `IsReCaptcha: true`. כל קריאה, חוץ מ-`getAboutHtml`, `GetAppealUrl` ו-`Luts/...`, דורשת טוקן reCAPTCHA v3.
- `robots.txt` אוסר `*.pdf`.
- **לא משתמשים בו (PRD §7).** את `pl_url` שומרים בדוח כקישור לפעולה ידנית. התוצאה: מסמכים של תכניות ארציות ומחוזיות (למשל `תמא/ 60`) שאין להן עותק ב-Complot יסומנו "דורש פעולה ידנית".

## 4. רישוי זמין ❌ לא נמצא ממשק ציבורי
- לפי תקנות רישוי בנייה (2016), כל בקשה מוגשת ומטופלת דרך המערכת. ההגשה נעשית על ידי עורך הבקשה, עם הזדהות.
- לא נמצא ממשק ציבורי לצפייה או חיפוש בלי התחברות. לפי תשובה של מינהל התכנון, המידע מונגש דרך אתרי ההנדסה של הרשויות.
- **בבני ברק זה מכוסה:** הכותרת של רשימת הבקשות ב-Complot היא "מספר בקשה (רישוי זמין)", ובעמוד בקשה יש שדה "מספר הבקשה ברישוי זמין". המסקנה: בגרסה 1 לא צריך `rishui_zamin.py`. אם תחליטו להשאיר אותו ב-PRD §5.1, הוא יהיה ריק.

## 5. עיריית בני ברק – Complot ✅

### 5.1 זיהוי
- העירייה מטמיעה את המערכת בעמוד "איתור בקשות והיתרי בניה": `https://www.bnei-brak.muni.il/תושבים/vadad-tichnun-bnya/request/`. בעמוד יש `var site_id = 75`.
- ספק: **Complot** ("handasi2016"). המסמכים נשמרים בארכיון של **gis-net**.
- Base: `https://handasi.complot.co.il/magicscripts/mgrqispi.dll?appname=cixpa&prgname=<op>&...`
- סוג: **קטעי HTML** (Bootstrap) שצריך לפרסר. בנוסף יש SOAP ב-`.../wsComplotPublicData/ComplotPublicData.asmx` לרשימות (רחובות, יישובים, גושים, חלקות).
- את שמות הפעולות והפרמטרים מצאתי בקבצים `handasi2016/Scripts/Complot/request/min/_routes.min.js` ו-`handasi2016/Scripts/wp/site.min.js`.
- אין התחברות ואין CAPTCHA. `robots.txt` לא קיים (השרת מחזיר עמוד שגיאה).

### 5.2 חלקה – `GetGushFile` ✅
```
prgname=GetGushFile&siteid=75&g=6123&h=2518&arguments=siteid,g,h
```
מחזיר: פרטי חלקה, כתובות, תכניות, זכויות והתראות.

לחלקה שעברה פרצלציה (`h=443`) מתקבל עמוד אחר: "החלקה המבוקשת עברה פרצלציה", עם טבלת חלקות בנות `getGush(6123,2518)` ו-`getGush(6123,2519)`, כולל כתובת, ייעוד ושטח.

### 5.3 חיפוש בקשות/היתרים ✅

| פעולה | פרמטרים | נבדק |
|---|---|---|
| `GetBakashotByGush` | `siteid=75&grp=0&t=0&g=<gush>&h=<helka>&l=true&arguments=siteId,grp,t,g,h,l` | 6123/443 → 4 בקשות. 6123/2518 → 0. גוש 6123 בלי חלקה → 1,242 (מציג 50) |
| `GetBakashotByAddress` | `siteid=75&grp=0&t=0&c=6100&s=<street code>&h=<house>&l=true&arguments=siteId,grp,t,c,s,h,l` | הבנים(423) 3 → **5 בקשות** |
| `GetBakashotByHeter` | `siteid=75&grp=0&t=0&b=<heter>&l=true&arguments=siteId,grp,t,b,l` | לא נבדק |
| `GetBakashotByTik` | `siteid=75&grp=0&t=0&k=<tik>&arguments=siteId,grp,t,k` | לא נבדק |

שורת תוצאה לדוגמה (הבנים 3): בקשה `195000038`, תיק בניין `61230443`, הוגשה 15/03/1950, "סריקה", גוש 6123, חלקה **443**, וכפתור `showRequestDocs(195000038)`.

> ⚠️ **היתרים ישנים רשומים על מספר החלקה הישן (443), לא על החלקה הבת (2518).** החיפוש לפי כתובת מצא בקשה אחת יותר מהחיפוש לפי חלקה. לכן החיפוש חייב לכלול: חלקה נוכחית + חלקת מקור + כתובת, ולאחד כפילויות לפי מספר בקשה. זה תואם ל-PRD §4.4.

### 5.4 פרטי בקשה – `GetBakashaFile` ✅
```
prgname=GetBakashaFile&siteid=75&b=20177&arguments=siteid,b
```
מחזיר: תיק בניין `61231012`, סוג "בקשה להיתר", שימוש "בית ספר", תיאור "תוספת בניה", **מספר היתר `9545`**, **תאריך הפקת היתר 23/03/1989**, מהות, בעלי עניין, גוש/חלקה/ייעוד, אירועים, החלטות ועדה, וקישור PDF לארכיון.

### 5.5 מסמכי בקשה – `GetBakashaDocs` ✅
```
prgname=GetBakashaDocs&siteid=75&b=195000038&arguments=siteid,b
```
מחזיר טבלה של שם, נושא, תאריך וקישור PDF:

| שם המסמך | נושא |
|---|---|
| בקשה 1435 - היתר 911 - תאריך 15/3/1950 | **היתר** |
| בקשה 1435 - היתר 911 - תאריך 15/3/1950 | **גרמושקה עם חותמת היתר** (= תשריט) |

PDF לדוגמה: HTTP 200, `application/pdf`, ‏272 KB. בקשה בלי שורות כאן תסומן "לא סרוק – לפנות לארכיון" (PRD §4.4).

### 5.6 מפת GIS עירונית – gis-net 🟡
- `https://v5.gis-net.co.il/v5/BneiBraq`. קישור התמקדות מתוך Complot: `https://v5.gis-net.co.il/v5api/Home/Focus?Customer=6100&FocusType=3&Where=<gush>,<helka>&AnswerType=1`.
- לא נבדק לעומק. לא נדרש לגרסה 1.

### 5.7 ארכיון הנדסה (לא מקוון)
- עמוד: `https://www.bnei-brak.muni.il/archiv-handasa/`. איש קשר וטלפון לפניות על תיקים שלא נסרקו.

---

## שאלות פתוחות

1. ~~robots.txt של `archive.gis-net.co.il`~~ **הוחלט (2026-10-28):** מורידים רק קבצים ספציפיים שהמשתמש בחר. אין זחילה, User-Agent מזהה, בקשה אחת בשנייה לכל היותר. ראו PRD §7.
2. **מבא"ת חסום ב-reCAPTCHA.** תכניות שאין להן עותק ב-Complot יסומנו "דורש פעולה ידנית" עם `pl_url`.
3. ~~`rishui_zamin.py`~~ **הוחלט (2026-10-28):** הוסר מגרסה 1. ה-PRD עודכן.
4. עימוד ב-`GetBakashotByGush`: ברירת המחדל מחזירה 50 תוצאות. בחיפוש לפי חלקה בודדת זה כנראה לא משנה. צריך לבדוק את `getUnlimitedSearch` אם יידרש.
5. שאילתת פוליגון ב-iplan (`esriGeometryPolygon`) לא נבדקה, רק נקודה.

## המלצה לשלב 1 (תב"עות ב-CLI)
1. `cadastre.py`: GovMap WFS עם `INTERSECTS`, ולכל חלקה בדיקת פרצלציה ב-Complot `GetGushFile` עם מעבר לחלקות הבנות.
2. `providers/iplan.py`: רשימת תכניות מאושרות לפי מיקום, עם `station_desc='אישור'`.
3. `providers/bnei_brak.py`: תכניות "בתוקף" מתוך `GetGushFile`, ומסמכים מתוך `GetTabaFile`. מורידים "תקנון" ו"תשריט" בגרסת "לתוקף".
4. איחוד כפילויות לפי מספר תכנית `501-...`. תכנית שקיימת רק ב-iplan תסומן "דורש פעולה ידנית" עם קישור למבא"ת.
5. הורדה מהארכיון: רק קבצים שהמשתמש בחר (שאלה 1).
