using System;
using System.IO;
using System.Threading.Tasks;
using Windows.Data.Json;
using Windows.Storage;

namespace pyToneRIP.Shared
{
    internal static class PreviewWorkflowStore
    {
        private static string SessionFile(string sessionId)
        {
            if (string.IsNullOrWhiteSpace(sessionId)) throw new InvalidDataException("Preview session identity is missing.");
            return QueueIdentity.FileToken("preview:" + sessionId, null) + ".json";
        }

        public static async Task PrepareAsync(StorageFolder job, string sessionId, JsonObject options)
        {
            var settings = JsonObject.Parse(options.Stringify());
            settings.SetNamedValue("version", JsonValue.CreateNumberValue(1));
            settings.SetNamedValue("job_id", JsonValue.CreateStringValue(job.Name));
            settings.SetNamedValue("session_id", JsonValue.CreateStringValue(sessionId));
            var path = settings.GetNamedString("icc_path", "");
            if (!string.IsNullOrWhiteSpace(path))
            {
                var profile = await ExternalIccAccessStore.OpenAsync(path);
                await profile.CopyAsync(job, "preview-printer.icc", NameCollisionOption.ReplaceExisting);
            }
            await WriteAtomicAsync(job, "preview-settings.json", settings.Stringify());
            var sessions = await SharedApplicationStorage.Root.CreateFolderAsync("preview-sessions", CreationCollisionOption.OpenIfExists);
            await WriteAtomicAsync(sessions, SessionFile(sessionId), settings.Stringify());
        }

        public static async Task<StorageFolder> OpenJobAsync(string sessionId)
        {
            var sessions = await SharedApplicationStorage.Root.GetFolderAsync("preview-sessions");
            var data = JsonObject.Parse(await FileIO.ReadTextAsync(await sessions.GetFileAsync(SessionFile(sessionId))));
            var id = data.GetNamedString("job_id", "");
            if (data.GetNamedString("session_id", "") != sessionId || !Guid.TryParse(id, out _))
                throw new InvalidDataException("Preview does not match this print session.");
            var jobs = await SharedApplicationStorage.Root.GetFolderAsync("jobs");
            return await jobs.GetFolderAsync(id);
        }

        public static async Task RemoveSessionAsync(string sessionId)
        {
            try
            {
                var sessions = await SharedApplicationStorage.Root.GetFolderAsync("preview-sessions");
                await (await sessions.GetFileAsync(SessionFile(sessionId))).DeleteAsync();
            }
            catch (FileNotFoundException) { }
        }

        public static async Task WriteAtomicAsync(StorageFolder folder, string name, string text)
        {
            var temporary = await folder.CreateFileAsync(name + ".tmp", CreationCollisionOption.ReplaceExisting);
            await FileIO.WriteTextAsync(temporary, text);
            await temporary.RenameAsync(name, NameCollisionOption.ReplaceExisting);
        }
    }
}
