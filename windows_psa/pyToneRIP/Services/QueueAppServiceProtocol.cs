using System.IO;
using System.Runtime.Serialization.Json;
using System.Text;

namespace pyToneRIP.Services
{
    internal static class QueueAppServiceProtocol
    {
        internal const string ClientService = "pytone-rip.queue.v1";
        internal const string HelperService = "pytone-rip.queue-helper.v1";
        internal const string LaunchGroup = "QueueAppService";
        internal const string LaunchArgument = "--queue-app-service";
        internal const string Request = "request";
        internal const string Response = "response";
        internal const string NotDispatched = "not_dispatched";
        internal const string Error = "error";
        internal const string ReplyTooLarge = "reply_too_large";
        internal const string ReplyFile = "reply_file";
        internal const string ExchangeFolder = "queue-helper";
        internal const string ReplyFileSuffix = ".appservice-response.json";
        // Stay comfortably under the broker's message-size limit even when
        // the JSON string is marshaled as UTF-16.
        internal const int MaxInlineJsonCharacters = 20000;

        internal static bool UsesAppService(string operation)
        {
            switch (operation)
            {
                case "list":
                case "discover-usb":
                case "read-usb-ink":
                case "read-queue-ink":
                case "read-usb-information":
                case "open-jobs":
                case "cancel-current-job":
                case "cancel-delivered-job":
                case "cancelable-job":
                case "current-job":
                case "repair-stale":
                case "rename":
                case "set-default":
                case "set-user-defaults":
                case "verify-user-defaults":
                case "read-user-defaults":
                case "read-configuration-ticket":
                case "notify-user-defaults":
                    return true;
                default:
                    return false;
            }
        }

        internal static bool IsReadOnly(string operation)
        {
            switch (operation)
            {
                case "list":
                case "discover-usb":
                case "read-usb-ink":
                case "read-queue-ink":
                case "read-usb-information":
                case "cancelable-job":
                case "current-job":
                case "verify-user-defaults":
                case "read-user-defaults":
                case "read-configuration-ticket":
                    return true;
                default:
                    return false;
            }
        }

        internal static string Serialize<T>(T value)
        {
            using (var stream = new MemoryStream())
            {
                new DataContractJsonSerializer(typeof(T)).WriteObject(stream, value);
                return Encoding.UTF8.GetString(stream.ToArray());
            }
        }

        internal static T Deserialize<T>(string value)
        {
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(value)))
                return (T)new DataContractJsonSerializer(typeof(T)).ReadObject(stream);
        }
    }
}
