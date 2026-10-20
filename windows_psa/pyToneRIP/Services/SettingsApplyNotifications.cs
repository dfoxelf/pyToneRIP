using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Threading.Tasks;
using Windows.Storage;

namespace pyToneRIP.Services
{
    [DataContract]
    internal sealed class SettingsApplyFailure
    {
        [DataMember(Name = "schema")] public string Schema { get; set; } = "pytone-rip.settings-apply-failure";
        [DataMember(Name = "version")] public int Version { get; set; } = 1;
        [DataMember(Name = "queue_name")] public string QueueName { get; set; }
        [DataMember(Name = "message")] public string Message { get; set; }
        [DataMember(Name = "details")] public string Details { get; set; }
        [DataMember(Name = "occurred_utc")] public string OccurredUtc { get; set; }
        internal StorageFile File { get; set; }
    }

    internal static class SettingsApplyNotifications
    {
        private const string FolderName = "settings-notifications";

        public static async Task ReportFailureAsync(string queueName, Exception error)
        {
            App.WriteStartupException(
                "RIP configuration saved; Windows preferences failed: queue=" + queueName, error);
            try { await PublishFailureAsync(queueName, error); }
            catch (Exception notificationError)
            {
                App.WriteStartupException("Could not publish saved-settings warning", notificationError);
            }
        }

        public static async Task PublishFailureAsync(string queueName, Exception error)
        {
            // Settings UI and Manager can be separate processes. Publish only a
            // completed notification; no queue settings or print objects live here.
            var folder = await Shared.SharedApplicationStorage.Root.CreateFolderAsync(
                FolderName, CreationCollisionOption.OpenIfExists);
            var name = DateTimeOffset.UtcNow.ToString("yyyyMMddTHHmmssfffffff", CultureInfo.InvariantCulture)
                + "-" + Guid.NewGuid().ToString("N");
            var file = await folder.CreateFileAsync(name + ".tmp", CreationCollisionOption.FailIfExists);
            using (var stream = await file.OpenStreamForWriteAsync())
            {
                new DataContractJsonSerializer(typeof(SettingsApplyFailure)).WriteObject(
                    stream, new SettingsApplyFailure
                    {
                        QueueName = queueName,
                        Message = error.Message,
                        Details = error.ToString(),
                        OccurredUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                    });
            }
            await file.RenameAsync(name + ".json", NameCollisionOption.FailIfExists);
        }

        public static async Task<IReadOnlyList<SettingsApplyFailure>> ReadPendingAsync()
        {
            StorageFolder folder;
            try { folder = await Shared.SharedApplicationStorage.Root.GetFolderAsync(FolderName); }
            catch (FileNotFoundException) { return Array.Empty<SettingsApplyFailure>(); }
            var pending = new List<SettingsApplyFailure>();
            foreach (var file in (await folder.GetFilesAsync()).Where(item =>
                item.Name.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
                .OrderBy(item => item.Name, StringComparer.Ordinal))
            {
                try
                {
                    SettingsApplyFailure notification;
                    using (var stream = await file.OpenStreamForReadAsync())
                        notification = (SettingsApplyFailure)new DataContractJsonSerializer(
                            typeof(SettingsApplyFailure)).ReadObject(stream);
                    if (notification?.Schema != "pytone-rip.settings-apply-failure"
                        || notification.Version != 1 || string.IsNullOrWhiteSpace(notification.Message))
                        throw new InvalidDataException("Invalid settings notification.");
                    notification.File = file;
                    pending.Add(notification);
                }
                catch (FileNotFoundException) { /* Another Manager acknowledged it. */ }
                catch (Exception error)
                {
                    App.WriteStartupException("Could not read settings notification", error);
                }
            }
            return pending;
        }

        public static async Task AcknowledgeAsync(SettingsApplyFailure notification)
        {
            try { await notification.File.DeleteAsync(); }
            catch (FileNotFoundException) { /* Another Manager acknowledged it. */ }
        }
    }
}
