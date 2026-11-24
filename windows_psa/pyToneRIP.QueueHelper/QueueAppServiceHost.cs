using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using pyToneRIP.Services;
using Windows.ApplicationModel;
using Windows.ApplicationModel.AppService;
using Windows.Foundation.Collections;

namespace pyToneRIP.QueueHelper
{
    internal sealed class QueueAppServiceHost
    {
        private readonly string localState;
        private readonly object gate = new object();
        private readonly TaskCompletionSource<bool> drained = new TaskCompletionSource<bool>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        private int activeRequests;
        private bool closed;

        internal QueueAppServiceHost(string localState) => this.localState = localState;

        internal async Task RunAsync()
        {
            using (var connection = new AppServiceConnection
            {
                AppServiceName = QueueAppServiceProtocol.HelperService,
                PackageFamilyName = Package.Current.Id.FamilyName,
            })
            {
                connection.RequestReceived += OnRequestReceived;
                connection.ServiceClosed += (_, __) =>
                {
                    lock (gate)
                    {
                        closed = true;
                        if (activeRequests == 0) drained.TrySetResult(true);
                    }
                };
                var status = await connection.OpenAsync();
                if (status != AppServiceConnectionStatus.Success)
                    throw new InvalidOperationException("QueueHelper AppService could not connect: " + status);
                await drained.Task;
            }
        }

        private async void OnRequestReceived(AppServiceConnection sender, AppServiceRequestReceivedEventArgs args)
        {
            var deferral = args.GetDeferral();
            lock (gate)
            {
                if (closed)
                {
                    deferral.Complete();
                    return;
                }
                activeRequests++;
            }
            string replyPath = null;
            var replyDelivered = false;
            try
            {
                ValueSet reply;
                try
                {
                    var json = args.Request.Message[QueueAppServiceProtocol.Request] as string;
                    var request = QueueAppServiceProtocol.Deserialize<QueueHelperRequest>(json);
                    if (!QueueAppServiceProtocol.UsesAppService(request?.Operation))
                        throw new InvalidOperationException("Unsupported queue AppService operation.");
                    // Win32 and USB APIs are synchronous. Each request gets its own
                    // work item, so an ink query cannot hold up Open Jobs.
                    var response = await Task.Run(() => Program.ExecuteAppServiceRequest(request, localState));
                    var responseJson = QueueAppServiceProtocol.Serialize(response);
                    if (responseJson.Length <= QueueAppServiceProtocol.MaxInlineJsonCharacters)
                        reply = new ValueSet { [QueueAppServiceProtocol.Response] = responseJson };
                    else
                    {
                        // Preserve the exact result without replaying a command
                        // that already changed Windows queue state.
                        var folder = Path.Combine(localState, QueueAppServiceProtocol.ExchangeFolder);
                        Directory.CreateDirectory(folder);
                        var token = Guid.NewGuid().ToString("N");
                        replyPath = Path.Combine(folder, token + QueueAppServiceProtocol.ReplyFileSuffix);
                        File.WriteAllText(replyPath, responseJson, new UTF8Encoding(false));
                        reply = new ValueSet { [QueueAppServiceProtocol.ReplyFile] = token };
                    }
                }
                catch (Exception error)
                {
                    Program.TryLog(localState, "Queue AppService request failed: " + error);
                    reply = new ValueSet { [QueueAppServiceProtocol.Error] = error.Message };
                }
                var status = await args.Request.SendResponseAsync(reply);
                replyDelivered = status == AppServiceResponseStatus.Success && reply.ContainsKey(QueueAppServiceProtocol.ReplyFile);
                if (status != AppServiceResponseStatus.Success)
                    Program.TryLog(localState, "Queue AppService could not deliver reply: " + status);
            }
            catch (Exception error)
            {
                Program.TryLog(localState, "Queue AppService reply failed: " + error);
            }
            finally
            {
                if (replyPath != null && !replyDelivered)
                {
                    try { File.Delete(replyPath); }
                    catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
                    {
                        Program.TryLog(localState, "Could not remove undelivered queue AppService reply: " + error);
                    }
                }
                deferral.Complete();
                lock (gate)
                {
                    if (--activeRequests == 0 && closed) drained.TrySetResult(true);
                }
            }
        }
    }
}
