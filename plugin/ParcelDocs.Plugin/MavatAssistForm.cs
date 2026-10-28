using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ParcelDocs.Plugin
{
    /// <summary>
    /// Completing plan documents from mavat by hand. The service opens a plan's mavat page
    /// in the browser; the user downloads the files there (and passes the CAPTCHA). Every
    /// refresh asks the service to look at the downloads folder and file what arrived.
    /// </summary>
    public sealed class MavatAssistForm : Form
    {
        private const int RefreshMilliseconds = 2000;

        private readonly ServiceClient _service;
        private readonly ListView _plans;
        private readonly ListView _unidentified;
        private readonly TextBox _log;
        private readonly Label _downloads;
        private readonly Button _open;
        private readonly Button _assign;
        private readonly Timer _timer;
        private string _folder;
        private int _filedShown;

        public MavatAssistForm(ServiceClient service, AssistStatusDto status)
        {
            _service = service;
            Text = "ParcelDocs – השלמת מסמכים ממבא\"ת";
            RightToLeft = RightToLeft.Yes;
            RightToLeftLayout = true;
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = false;
            ShowInTaskbar = false;
            Font = new Font("Segoe UI", 9.5f);
            AutoScaleMode = AutoScaleMode.Font;
            ClientSize = new Size(1000, 680);
            MinimumSize = new Size(760, 520);

            var help = new Label
            {
                AutoSize = true,
                MaximumSize = new Size(960, 0),
                Margin = new Padding(6, 8, 6, 4),
                Text = "1. בחר תכנית ולחץ \"פתח במבא\"ת\". דף התכנית ייפתח בדפדפן.\n" +
                       "2. הורד את ההוראות והתשריט (PDF או ZIP). אם מופיע אימות \"אני לא רובוט\", השלם אותו בדפדפן.\n" +
                       "3. הקבצים מזוהים בתיקיית ההורדות, מקבלים שם (הוראות / תשריט / נספח) ומתויקים בפרויקט ובספרייה.",
            };
            _downloads = new Label { AutoSize = true, Margin = new Padding(6, 0, 6, 6), ForeColor = Color.DimGray };

            _plans = new ListView
            {
                Dock = DockStyle.Fill,
                View = View.Details,
                FullRowSelect = true,
                GridLines = true,
                HideSelection = false,
                MultiSelect = false,
                RightToLeftLayout = true,
            };
            _plans.Columns.Add("מספר", 130);
            _plans.Columns.Add("שם", 360);
            _plans.Columns.Add("הוראות", 70);
            _plans.Columns.Add("תשריט", 70);
            _plans.Columns.Add("קבצים", 70);
            _plans.Columns.Add("מצב", 140);
            _plans.SelectedIndexChanged += (s, e) => UpdateButtons();
            _plans.DoubleClick += (s, e) => OpenSelected();

            _open = MakeButton("פתח במבא\"ת");
            _open.Click += (s, e) => OpenSelected();
            var refresh = MakeButton("רענן");
            refresh.Click += (s, e) => Refresh(() => _service.MavatStatus());
            var openFolder = MakeButton("פתח תיקיית פרויקט");
            openFolder.Click += (s, e) => OpenFolder();
            var planButtons = Row(_open, refresh, openFolder);

            var unidentifiedTitle = new Label
            {
                AutoSize = true,
                Margin = new Padding(6, 8, 6, 2),
                Text = "קבצים שלא זוהו (סמן קובץ ותכנית ולחץ \"שייך\"):",
            };
            _unidentified = new ListView
            {
                Dock = DockStyle.Fill,
                View = View.Details,
                FullRowSelect = true,
                GridLines = true,
                HideSelection = false,
                MultiSelect = false,
                RightToLeftLayout = true,
            };
            _unidentified.Columns.Add("קובץ", 380);
            _unidentified.Columns.Add("סיבה", 460);
            _unidentified.SelectedIndexChanged += (s, e) => UpdateButtons();
            _assign = MakeButton("שייך לתכנית המסומנת");
            _assign.Click += (s, e) => AssignSelected();
            var assignButtons = Row(_assign);

            _log = new TextBox
            {
                Dock = DockStyle.Fill,
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
            };

            var close = MakeButton("סיום");
            close.DialogResult = DialogResult.OK;
            CancelButton = close;
            var bottom = Row(close);

            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 9 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 50));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 20));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 30));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(help, 0, 0);
            layout.Controls.Add(_downloads, 0, 1);
            layout.Controls.Add(_plans, 0, 2);
            layout.Controls.Add(planButtons, 0, 3);
            layout.Controls.Add(unidentifiedTitle, 0, 4);
            layout.Controls.Add(_unidentified, 0, 5);
            layout.Controls.Add(assignButtons, 0, 6);
            layout.Controls.Add(_log, 0, 7);
            layout.Controls.Add(bottom, 0, 8);
            Controls.Add(layout);

            _timer = new Timer { Interval = RefreshMilliseconds };
            _timer.Tick += (s, e) => Refresh(() => _service.MavatStatus());
            Shown += (s, e) => _timer.Start();
            FormClosing += (s, e) =>
            {
                _timer.Stop();
                try
                {
                    _service.MavatStop();
                }
                catch (Exception ex) when (IsServiceError(ex))
                {
                    // The service may already be gone; nothing is left to clean up here.
                }
            };

            Apply(status);
            SelectFirstMissing();
        }

        private static Button MakeButton(string text)
        {
            return new Button
            {
                Text = text,
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                MinimumSize = new Size(110, 32),
                Padding = new Padding(10, 2, 10, 2),
            };
        }

        private static FlowLayoutPanel Row(params Control[] controls)
        {
            var row = new FlowLayoutPanel
            {
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                Margin = new Padding(3),
            };
            row.Controls.AddRange(controls);
            return row;
        }

        private static bool IsServiceError(Exception ex)
        {
            return ex is ServiceException || ex is HttpRequestException || ex is TaskCanceledException;
        }

        private void Refresh(Func<AssistStatusDto> call)
        {
            try
            {
                Apply(call());
            }
            catch (Exception ex) when (IsServiceError(ex))
            {
                _timer.Stop();
                MessageBox.Show(this, "השירות לא ענה:\n" + ex.Message, Text, MessageBoxButtons.OK,
                    MessageBoxIcon.Error, MessageBoxDefaultButton.Button1,
                    MessageBoxOptions.RtlReading | MessageBoxOptions.RightAlign);
            }
        }

        private void Apply(AssistStatusDto status)
        {
            _folder = status.Folder;
            _downloads.Text = "תיקיית ההורדות שנבדקת: " + status.DownloadsDir;

            string selectedPlan = SelectedPlan()?.Id;
            _plans.BeginUpdate();
            _plans.Items.Clear();
            foreach (var plan in status.Plans ?? new List<AssistPlanDto>())
            {
                string state = plan.Done ? "הושלם" : plan.Id == status.Active ? "פתוח במבא\"ת" : "חסר";
                if (!plan.Done && string.IsNullOrEmpty(plan.MavatUrl))
                {
                    state = "אין קישור למבא\"ת";
                }
                var item = new ListViewItem(new[]
                {
                    plan.Number, plan.Name ?? "", plan.HasInstructions ? "✓" : "–", plan.HasMap ? "✓" : "–",
                    (plan.Files?.Count ?? 0).ToString(), state,
                })
                {
                    Tag = plan,
                    ForeColor = plan.Done ? Color.DarkGreen : Color.DarkOrange,
                    ToolTipText = string.Join("\n", plan.Files ?? new List<string>()),
                };
                _plans.Items.Add(item);
                item.Selected = plan.Id == selectedPlan;
            }
            _plans.EndUpdate();

            string selectedFile = SelectedFile()?.Name;
            _unidentified.BeginUpdate();
            _unidentified.Items.Clear();
            foreach (var file in status.Unidentified ?? new List<UnidentifiedFileDto>())
            {
                var item = new ListViewItem(new[] { file.Name, file.Reason ?? "" }) { Tag = file };
                _unidentified.Items.Add(item);
                item.Selected = file.Name == selectedFile;
            }
            _unidentified.EndUpdate();

            var filed = status.Filed ?? new List<FiledFileDto>();
            foreach (var file in filed.Skip(_filedShown))
            {
                string note = file.Duplicate ? " (כבר קיים)" : file.InLibrary ? " + ספרייה" : "";
                _log.AppendText($"✓ {file.Name} ← {file.Kind}, תכנית {file.PlanNumber} ({file.Reason}){note}" +
                                Environment.NewLine);
            }
            _filedShown = filed.Count;
            UpdateButtons();
        }

        private void SelectFirstMissing()
        {
            foreach (ListViewItem item in _plans.Items)
            {
                if (!((AssistPlanDto)item.Tag).Done)
                {
                    item.Selected = true;
                    item.EnsureVisible();
                    break;
                }
            }
        }

        private AssistPlanDto SelectedPlan()
        {
            return _plans.SelectedItems.Count > 0 ? (AssistPlanDto)_plans.SelectedItems[0].Tag : null;
        }

        private UnidentifiedFileDto SelectedFile()
        {
            return _unidentified.SelectedItems.Count > 0
                ? (UnidentifiedFileDto)_unidentified.SelectedItems[0].Tag
                : null;
        }

        private void UpdateButtons()
        {
            var plan = SelectedPlan();
            _open.Enabled = plan != null && !string.IsNullOrEmpty(plan.MavatUrl);
            _assign.Enabled = plan != null && SelectedFile() != null;
        }

        private void OpenSelected()
        {
            var plan = SelectedPlan();
            if (plan == null || string.IsNullOrEmpty(plan.MavatUrl))
            {
                return;
            }
            Refresh(() => _service.MavatOpen(plan.Id));
        }

        private void AssignSelected()
        {
            var plan = SelectedPlan();
            var file = SelectedFile();
            if (plan == null || file == null)
            {
                return;
            }
            Refresh(() => _service.MavatAssign(file.Name, plan.Id));
        }

        private void OpenFolder()
        {
            if (!string.IsNullOrEmpty(_folder) && Directory.Exists(_folder))
            {
                Process.Start(new ProcessStartInfo { FileName = _folder, UseShellExecute = true, Verb = "open" });
            }
        }
    }
}
