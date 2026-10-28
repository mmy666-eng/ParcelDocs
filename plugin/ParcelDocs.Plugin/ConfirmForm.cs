using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Windows.Forms;

namespace ParcelDocs.Plugin
{
    /// <summary>Lets the user keep or drop plans and permits and name the project (PRD §3 step 4).</summary>
    public sealed class ConfirmForm : Form
    {
        // Upper bound for the name column so long plan names don't push the notes off screen.
        private const int MaxNameColumnWidth = 420;

        private readonly ListView _items;
        private readonly TextBox _project;

        public ConfirmForm(ResolveResponse result, string defaultProject)
        {
            Text = "ParcelDocs – אישור הורדה";
            RightToLeft = RightToLeft.Yes;
            RightToLeftLayout = true;
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = false;
            ShowInTaskbar = false;
            Font = new Font("Segoe UI", 9.5f);
            AutoScaleMode = AutoScaleMode.Font;
            ClientSize = new Size(1150, 640);
            MinimumSize = new Size(800, 450);

            var parcels = string.Join(", ", result.Parcels.Select(p =>
            {
                var address = string.Join(", ", (p.Addresses ?? new List<AddressDto>())
                    .Select(a => $"{a.Street} {a.Number}".Trim()));
                return address.Length > 0 ? $"{p.Gush}/{p.Helka} ({address})" : $"{p.Gush}/{p.Helka}";
            }));
            var header = new Label
            {
                AutoSize = true,
                Text = $"חלקות: {parcels}",
                Margin = new Padding(6, 8, 6, 4),
            };

            var projectPanel = new FlowLayoutPanel
            {
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                WrapContents = false,
                Margin = new Padding(3),
            };
            projectPanel.Controls.Add(new Label
            {
                Text = "שם הפרויקט:",
                AutoSize = true,
                Anchor = AnchorStyles.Left | AnchorStyles.Right,
                Margin = new Padding(3, 7, 3, 3),
            });
            // Editable; the drawing name is only the default.
            _project = new TextBox { Width = 450, Text = defaultProject, ReadOnly = false, Margin = new Padding(3, 4, 3, 3) };
            projectPanel.Controls.Add(_project);

            _items = new ListView
            {
                Dock = DockStyle.Fill,
                View = View.Details,
                CheckBoxes = true,
                FullRowSelect = true,
                GridLines = true,
                RightToLeftLayout = true,
            };
            _items.Columns.Add("סוג");
            _items.Columns.Add("מספר");
            _items.Columns.Add("שם / תיאור");
            _items.Columns.Add("תאריך");
            _items.Columns.Add("מס' מסמכים");
            _items.Columns.Add("מקור");
            _items.Columns.Add("הערה");
            foreach (var plan in result.Plans)
            {
                AddItem(plan.Id, "תב\"ע", plan.Number, plan.Name, plan.ApprovalDate, plan.Documents,
                    SourceLabel(plan.DocumentsSource), plan.ManualAction);
            }
            foreach (var permit in result.Permits)
            {
                AddItem(permit.Id, "היתר", permit.PermitNumber ?? permit.RequestNumber, permit.Description,
                    permit.Date, permit.Documents, permit.Documents > 0 ? SourceLabel("bnei_brak") : "",
                    permit.ManualAction);
            }
            FitColumns();

            bool hasWarnings = result.Warnings != null && result.Warnings.Count > 0;
            var warnings = new TextBox
            {
                Dock = DockStyle.Fill,
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                Text = string.Join(Environment.NewLine, (result.Warnings ?? new List<string>()).Select(w => "⚠ " + w)),
                Visible = hasWarnings,
            };

            var buttons = new FlowLayoutPanel
            {
                AutoSize = true,
                AutoSizeMode = AutoSizeMode.GrowAndShrink,
                FlowDirection = FlowDirection.LeftToRight,
                WrapContents = false,
                Margin = new Padding(6),
            };
            var ok = MakeButton("הורדה");
            ok.DialogResult = DialogResult.OK;
            var cancel = MakeButton("ביטול");
            cancel.DialogResult = DialogResult.Cancel;
            var all = MakeButton("סמן הכל");
            var none = MakeButton("בטל הכל");
            all.Click += (s, e) => SetAll(true);
            none.Click += (s, e) => SetAll(false);
            buttons.Controls.AddRange(new Control[] { ok, cancel, all, none });
            AcceptButton = ok;
            CancelButton = cancel;
            ok.Click += (s, e) =>
            {
                if (string.IsNullOrWhiteSpace(_project.Text))
                {
                    MessageBox.Show(this, "יש להזין שם פרויקט.", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning,
                        MessageBoxDefaultButton.Button1, MessageBoxOptions.RtlReading | MessageBoxOptions.RightAlign);
                    DialogResult = DialogResult.None;
                }
            };

            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.RowStyles.Add(hasWarnings ? new RowStyle(SizeType.Absolute, 90) : new RowStyle(SizeType.Absolute, 0));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(header, 0, 0);
            layout.Controls.Add(projectPanel, 0, 1);
            layout.Controls.Add(_items, 0, 2);
            layout.Controls.Add(warnings, 0, 3);
            layout.Controls.Add(buttons, 0, 4);
            Controls.Add(layout);

            Shown += (s, e) =>
            {
                _project.Focus();
                _project.SelectAll();
            };
        }

        public string ProjectName => _project.Text.Trim();

        public IList<string> SelectedIds =>
            _items.CheckedItems.Cast<ListViewItem>().Select(item => (string)item.Tag).ToList();

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

        /// <summary>Each column as wide as the wider of its header and its content.</summary>
        private void FitColumns()
        {
            foreach (ColumnHeader column in _items.Columns)
            {
                column.AutoResize(ColumnHeaderAutoResizeStyle.HeaderSize);
                int header = column.Width;
                if (_items.Items.Count > 0)
                {
                    column.AutoResize(ColumnHeaderAutoResizeStyle.ColumnContent);
                }
                column.Width = Math.Max(column.Width, header);
            }
            ColumnHeader name = _items.Columns[2];
            name.Width = Math.Min(name.Width, MaxNameColumnWidth);
        }

        /// <summary>Where the documents come from, as the office calls it.</summary>
        public static string SourceLabel(string source)
        {
            switch (source)
            {
                case "library": return "ספרייה";
                case "rmi": return "רמ\"י";
                case "bnei_brak": return "עירייה";
                case "mavat": return "מבא\"ת";
                default: return source ?? "";
            }
        }

        private void AddItem(string id, string kind, string number, string name, string date, int documents,
            string source, string note)
        {
            var item = new ListViewItem(new[]
            {
                kind, number ?? "", name ?? "", date ?? "", documents.ToString(), source ?? "", note ?? "",
            })
            {
                Tag = id,
                Checked = true,
                ToolTipText = note ?? "",
            };
            if (!string.IsNullOrEmpty(note))
            {
                item.ForeColor = Color.DarkOrange;
            }
            _items.Items.Add(item);
        }

        private void SetAll(bool value)
        {
            foreach (ListViewItem item in _items.Items)
            {
                item.Checked = value;
            }
        }
    }
}
