using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;
// The only CAD-specific code in the plugin: AutoCAD and ZWCAD share the same API
// shape under different namespaces (PRD §5.4).
#if ZWCAD
using ZwSoft.ZwCAD.ApplicationServices;
using ZwSoft.ZwCAD.DatabaseServices;
using ZwSoft.ZwCAD.EditorInput;
using ZwSoft.ZwCAD.Runtime;
using CadApp = ZwSoft.ZwCAD.ApplicationServices.Application;
#else
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using CadApp = Autodesk.AutoCAD.ApplicationServices.Application;
#endif

[assembly: CommandClass(typeof(ParcelDocs.Plugin.Commands))]

namespace ParcelDocs.Plugin
{
    public class Commands
    {
        private const string ServiceNotRunning =
            "שירות ParcelDocs לא פועל.\n\n" +
            "יש להפעיל אותו מתוך תיקיית service:\n" +
            ".venv\\Scripts\\python -m app.main\n\n" +
            "ולהריץ שוב את PARCELDOCS.";

        [CommandMethod("PARCELDOCS", CommandFlags.Modal)]
        public void ParcelDocs()
        {
            Document doc = CadApp.DocumentManager.MdiActiveDocument;
            if (doc == null)
            {
                return;
            }
            Editor ed = doc.Editor;

            using (var service = new ServiceClient())
            {
                if (!service.IsRunning())
                {
                    ShowMessage(ServiceNotRunning, MessageBoxIcon.Error);
                    return;
                }

                List<double[]> vertices = SelectClosedPolyline(doc);
                if (vertices == null)
                {
                    return;
                }

                ed.WriteMessage($"\nParcelDocs: מחפש חלקות, תב\"עות והיתרים ({vertices.Count} קודקודים)... ");
                ResolveResponse result;
                try
                {
                    result = service.Resolve(vertices);
                }
                catch (System.Exception ex) when (ex is ServiceException || ex is System.Net.Http.HttpRequestException)
                {
                    ShowMessage("החיפוש נכשל:\n" + ex.Message, MessageBoxIcon.Error);
                    return;
                }
                ed.WriteMessage(
                    $"נמצאו {result.Parcels.Count} חלקות, {result.Plans.Count} תב\"עות, {result.Permits.Count} היתרים.");

                string defaultProject = Path.GetFileNameWithoutExtension(doc.Name);
                IList<string> selected;
                string project;
                using (var form = new ConfirmForm(result, defaultProject))
                {
                    if (CadApp.ShowModalDialog(form) != DialogResult.OK)
                    {
                        ed.WriteMessage("\nParcelDocs: בוטל.");
                        return;
                    }
                    selected = form.SelectedIds;
                    project = form.ProjectName;
                }

                ed.WriteMessage($"\nParcelDocs: מוריד {selected.Count} פריטים... ");
                DownloadResponse downloaded;
                try
                {
                    downloaded = service.Download(result.ResolveId, project, selected);
                }
                catch (System.Exception ex) when (ex is ServiceException || ex is System.Net.Http.HttpRequestException)
                {
                    ShowMessage("ההורדה נכשלה:\n" + ex.Message, MessageBoxIcon.Error);
                    return;
                }
                ed.WriteMessage($"הורדו {downloaded.Files} קבצים לתיקייה {downloaded.Folder}");

                string summary =
                    $"הורדו {downloaded.Files} קבצים ({downloaded.FromCache} מה-cache).\n" +
                    $"תב\"עות: {downloaded.Plans}, היתרים: {downloaded.Permits}\n\n{downloaded.Folder}";
                if (downloaded.Warnings != null && downloaded.Warnings.Count > 0)
                {
                    summary += "\n\n" + string.Join("\n", downloaded.Warnings.Select(w => "⚠ " + w));
                }
                string folder = Path.GetFullPath(downloaded.Folder)
                    .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                if (ShowMessage(summary + "\n\nלפתוח את התיקייה?", MessageBoxIcon.Information,
                        MessageBoxButtons.YesNo) == DialogResult.Yes && Directory.Exists(folder))
                {
                    // Shell-open the project folder itself rather than passing it to explorer.exe.
                    Process.Start(new ProcessStartInfo { FileName = folder, UseShellExecute = true, Verb = "open" });
                }
            }
        }

        /// <summary>Vertices in WCS (ITM metres) of a closed lightweight polyline, or null.</summary>
        private static List<double[]> SelectClosedPolyline(Document doc)
        {
            Editor ed = doc.Editor;
            var options = new PromptEntityOptions("\nבחר פוליליין סגור (קואורדינטות ITM): ");
            options.SetRejectMessage("\nיש לבחור פוליליין (LWPOLYLINE).");
            options.AddAllowedClass(typeof(Polyline), true);
            PromptEntityResult picked = ed.GetEntity(options);
            if (picked.Status != PromptStatus.OK)
            {
                return null;
            }

            using (Transaction tr = doc.TransactionManager.StartTransaction())
            {
                var polyline = (Polyline)tr.GetObject(picked.ObjectId, OpenMode.ForRead);
                var vertices = new List<double[]>();
                for (int i = 0; i < polyline.NumberOfVertices; i++)
                {
                    var point = polyline.GetPoint3dAt(i);
                    vertices.Add(new[] { point.X, point.Y });
                }
                bool endsAtStart = vertices.Count > 1 &&
                    polyline.GetPoint3dAt(0).DistanceTo(polyline.GetPoint3dAt(vertices.Count - 1)) < 1e-6;
                if (!polyline.Closed && !endsAtStart)
                {
                    ed.WriteMessage("\nהפוליליין אינו סגור. יש לסגור אותו (PEDIT > Close) ולנסות שוב.");
                    return null;
                }
                if (endsAtStart)
                {
                    vertices.RemoveAt(vertices.Count - 1);
                }
                if (vertices.Count < 3)
                {
                    ed.WriteMessage("\nלפוליליין צריכים להיות לפחות 3 קודקודים.");
                    return null;
                }
                if (polyline.HasBulges)
                {
                    ed.WriteMessage("\nשים לב: קשתות בפוליליין מוחלפות בקווים ישרים בין הקודקודים.");
                }
                tr.Commit();
                return vertices;
            }
        }

        private static DialogResult ShowMessage(string text, MessageBoxIcon icon,
            MessageBoxButtons buttons = MessageBoxButtons.OK)
        {
            return MessageBox.Show(text, "ParcelDocs", buttons, icon, MessageBoxDefaultButton.Button1,
                MessageBoxOptions.RtlReading | MessageBoxOptions.RightAlign);
        }
    }
}
