using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Windows.ApplicationModel.Resources;
using Windows.UI.Xaml;
using Windows.UI.Xaml.Controls;

namespace pyToneRIP.Services
{
    // x:Uid resolves every property while InitializeComponent is still blocking
    // the first frame. Keep the same RESW keys, but resolve this page's strings
    // on a view-independent worker and apply them after the page is visible.
    public abstract class DeferredLocalization : DependencyObject
    {
        internal sealed class Entry
        {
            internal FrameworkElement Element;
            internal string Key;
        }

        private sealed class PendingProperty
        {
            internal string ResourceKey;
            internal object OriginalValue;
            internal Func<object> Read;
            internal Action<string> Write;
        }

        private static List<Entry> capture;

        public static readonly DependencyProperty KeyProperty = DependencyProperty.RegisterAttached(
            "Key", typeof(string), typeof(DeferredLocalization),
            new PropertyMetadata(null, KeyChanged));

        public static string GetKey(DependencyObject element) =>
            (string)element.GetValue(KeyProperty);

        public static void SetKey(DependencyObject element, string value) =>
            element.SetValue(KeyProperty, value);

        internal static List<Entry> BeginCapture()
        {
            if (capture != null)
                throw new InvalidOperationException("A localization capture is already active.");
            capture = new List<Entry>();
            return capture;
        }

        internal static void EndCapture(List<Entry> entries)
        {
            if (ReferenceEquals(capture, entries))
                capture = null;
        }

        private static void KeyChanged(DependencyObject sender, DependencyPropertyChangedEventArgs args)
        {
            if (capture != null && sender is FrameworkElement element && args.NewValue is string key)
                capture.Add(new Entry { Element = element, Key = key });
        }

        internal static async Task ApplyAsync(IReadOnlyList<Entry> entries)
        {
            var pending = new List<PendingProperty>();
            foreach (var entry in entries)
            {
                var element = entry.Element;
                var key = entry.Key;
                if (element is TextBlock text)
                    Add(pending, key, "Text", () => text.Text, value => text.Text = value);
                else if (element is TextBox textBox)
                {
                    Add(pending, key, "Header", () => textBox.Header, value => textBox.Header = value);
                    if (!string.IsNullOrEmpty(textBox.PlaceholderText))
                        Add(pending, key, "PlaceholderText", () => textBox.PlaceholderText,
                            value => textBox.PlaceholderText = value);
                }
                else if (element is PivotItem pivot)
                    Add(pending, key, "Header", () => pivot.Header, value => pivot.Header = value);
                else if (element is ComboBox combo)
                    Add(pending, key, "Header", () => combo.Header, value => combo.Header = value);
                else if (element is ToggleSwitch toggle)
                    Add(pending, key, "Header", () => toggle.Header, value => toggle.Header = value);
                else if (element is ContentControl control)
                    Add(pending, key, "Content", () => control.Content, value => control.Content = value);
            }

            var keys = pending.Select(item => item.ResourceKey).Distinct().ToArray();
            var translations = await Task.Run(() =>
            {
                var loader = ResourceLoader.GetForViewIndependentUse();
                var values = new Dictionary<string, string>();
                foreach (var key in keys)
                {
                    var translated = loader.GetString(key);
                    if (!string.IsNullOrEmpty(translated))
                        values[key] = translated;
                }
                return values;
            });

            foreach (var item in pending)
            {
                // A capability load may replace a loading label while the
                // resources are resolving; never overwrite that newer value.
                if (translations.TryGetValue(item.ResourceKey, out var value)
                    && object.Equals(item.Read(), item.OriginalValue))
                    item.Write(value);
            }
        }

        private static void Add(List<PendingProperty> pending, string key, string property,
            Func<object> read, Action<string> write)
        {
            pending.Add(new PendingProperty
            {
                ResourceKey = key + "/" + property,
                OriginalValue = read(),
                Read = read,
                Write = write,
            });
        }
    }
}
