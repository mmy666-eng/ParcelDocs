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
        private readonly ListView _items;
        private readonly TextBox _project;

        public ConfirmForm(ResolveResponse result, string defaultProject)
        {
            Text = "ParcelDocs – אישור הורדה";
            RightToLeft = RightToLeft.Yes;
            RightToLeftLayout = true;
            StartPosition = FormStartPosition.CenterParent;
            MinimizeBox = false;
            MaximizeBox = false;
            ShowInTaskbar = false;
            Size = new Size(900, 600);
            Font = new Font("Segoe UI", 9.5f);

            var parcels = string.Join(", ", result.Parcels.Select(p =>
            {
                var address = string.Join(", ", (p.Addresses ?? new List<AddressDto>())
                    .Select(a => $"{a.Street} {a.Number}".Trim()));
                return address.Length > 0 ? $"{p.Gush}/{p.Helka} ({address})" : $"{p.Gush}/{p.Helka}";
            }));
            var header = new Label
            {
                Dock = DockStyle.Top,
                Height = 28,
                Text = $"חלקות: {parcels}",
                Padding = new Padding(6, 6, 6, 0),
            };

            var projectPanel = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 34, Padding = new Padding(4) };
            projectPanel.Controls.Add(new Label { Text = "שם הפרויקט:", AutoSize = true, Margin = new Padding(3, 7, 3, 3) });
            _project = new TextBox { Width = 300, Text = defaultProject };
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
            _items.Columns.Add("סוג", 70);
            _items.Columns.Add("מספר", 120);
            _items.Columns.Add("שם / תיאור", 330);
            _items.Columns.Add("תאריך", 90);
            _items.Columns.Add("מסמכים", 65);
            _items.Columns.Add("הערה", 200);
            foreach (var plan in result.Plans)
            {
                AddItem(plan.Id, "תב\"ע", plan.Number, plan.Name, plan.ApprovalDate, plan.Documents, plan.ManualAction);
            }
            foreach (var permit in result.Permits)
            {
                AddItem(permit.Id, "היתר", permit.PermitNumber ?? permit.RequestNumber, permit.Description,
                    permit.Date, permit.Documents, permit.ManualAction);
            }

            var warnings = new TextBox
            {
                Dock = DockStyle.Bottom,
                Height = 70,
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                Text = string.Join(Environment.NewLine, (result.Warnings ?? new List<string>()).Select(w => "⚠ " + w)),
                Visible = result.Warnings != null && result.Warnings.Count > 0,
            };

            var buttons = new FlowLayoutPanel
            {
                Dock = DockStyle.Bottom,
                Height = 42,
                FlowDirection = FlowDirection.LeftToRight,
                Padding = new Padding(6),
            };
            var ok = new Button { Text = "הורדה", DialogResult = DialogResult.OK, Width = 100 };
            var cancel = new Button { Text = "ביטול", DialogResult = DialogResult.Cancel, Width = 100 };
            var all = new Button { Text = "סמן הכל", Width = 100 };
            var none = new Button { Text = "בטל הכל", Width = 100 };
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

            Controls.Add(_items);
            Controls.Add(warnings);
            Controls.Add(buttons);
            Controls.Add(projectPanel);
            Controls.Add(header);
        }

        public string ProjectName => _project.Text.Trim();

        public IList<string> SelectedIds =>
            _items.CheckedItems.Cast<ListViewItem>().Select(item => (string)item.Tag).ToList();

        private void AddItem(string id, string kind, string number, string name, string date, int documents, string note)
        {
            var item = new ListViewItem(new[]
            {
                kind, number ?? "", name ?? "", date ?? "", documents.ToString(), note ?? "",
            })
            {
                Tag = id,
                Checked = true,
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
