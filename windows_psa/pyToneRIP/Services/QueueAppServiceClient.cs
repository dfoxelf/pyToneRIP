using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Windows.ApplicationModel;
using Windows.ApplicationModel.AppService;
using Windows.Foundation.Collections;
using Windows.Storage;

namespace pyToneRIP.Services
{
    internal static class QueueAppServiceClient
    {
        private static readonly SemaphoreSlim ConnectLock = new SemaphoreSlim(1);
        private static AppServiceConnection connection;

        internal static async Task<QueueHelperResponse> SendAsync(QueueHelperRequest request, TimeSpan timeout)
        {
            using (var deadline = new CancellationTokenSource(timeout))
            {
                var current = await ConnectAsync(deadline.Token);
                if (current == null)
                    return null;

                // Once submitted, a missing reply does not prove the helper
                // did not execute the operation. Never replay a write or cancel.
                AppServiceResponse reply;
                try
                {
                    reply = await current.SendMessageAsync(new ValueSet
                    {
                        [QueueAppServiceProtocol.Request] = QueueAppServiceProtocol.Serialize(request),
                    }).AsTask(deadline.Token);
                }
                catch (OperationCanceledException)
                {
                    throw new TimeoutException("Queue AppService did not respond: " + request.Operation);
                }
                if (reply.Status == AppServiceResponseStatus.Success && reply.Message != null)
                {
                    var message = reply.Message;
                    if (message.TryGetValue(QueueAppServiceProtocol.Response, out object value) && value is string json)
                        return QueueAppServiceProtocol.Deserialize<QueueHelperResponse>(json)
                            ?? throw new InvalidDataException("Queue AppService returned an empty response.");
                    if (message.TryGetValue(QueueAppServiceProtocol.ReplyFile, out object file) && file is string token)
                        return await ReadLargeReplyAsync(token);
                    if (message.TryGetValue(QueueAppServiceProtocol.NotDispatched, out object skipped) && skipped is bool notSent && notSent)
                        return null;
                    if (message.TryGetValue(QueueAppServiceProtocol.ReplyTooLarge, out object size) && size is bool tooLarge && tooLarge
                        && QueueAppServiceProtocol.IsReadOnly(request.Operation))
                        return null;
                    if (message.TryGetValue(QueueAppServiceProtocol.Error, out object error) && error is string detail)
                        throw new IOException(detail);
                }
                if (reply.Status == AppServiceResponseStatus.MessageSizeTooLarge
                    && QueueAppServiceProtocol.IsReadOnly(request.Operation))
                    return null;
                throw new IOException("Queue AppService response unavailable (" + request.Operation + "): " + reply.Status);
            }
        }

        private static async Task<QueueHelperResponse> ReadLargeReplyAsync(string token)
        {
            if (!Guid.TryParseExact(token, "N", out _))
                throw new InvalidDataException("Queue AppService returned an invalid response file ID.");
            var path = Path.Combine(ApplicationData.Current.LocalFolder.Path, QueueAppServiceProtocol.ExchangeFolder,
                token + QueueAppServiceProtocol.ReplyFileSuffix);
            try
            {
                var file = await StorageFile.GetFileFromPathAsync(path);
                return QueueAppServiceProtocol.Deserialize<QueueHelperResponse>(await FileIO.ReadTextAsync(file))
                    ?? throw new InvalidDataException("Queue AppService returned an empty response file.");
            }
            finally
            {
                try { File.Delete(path); }
                catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
                {
                    ManagerLogger.WriteException("Cleaning up queue AppService response", error);
                }
            }
        }

        private static async Task<AppServiceConnection> ConnectAsync(CancellationToken cancellation)
        {
            await ConnectLock.WaitAsync(cancellation);
            try
            {
                var existing = Volatile.Read(ref connection);
                if (existing != null)
                    return existing;
                var pending = new AppServiceConnection
                {
                    AppServiceName = QueueAppServiceProtocol.ClientService,
                    PackageFamilyName = Package.Current.Id.FamilyName,
                };
                pending.ServiceClosed += OnClosed;
                Interlocked.Exchange(ref connection, pending);
                try
                {
                    var status = await pending.OpenAsync().AsTask(cancellation);
                    if (status == AppServiceConnectionStatus.Success)
                        return pending;
                    ManagerLogger.Write("Queue AppService unavailable before dispatch: " + status);
                }
                catch (Exception error)
                {
                    ManagerLogger.WriteException("Connecting to queue AppService", error);
                }
                Close(pending);
                return null;
            }
            finally
            {
                ConnectLock.Release();
            }
        }

        private static void OnClosed(AppServiceConnection sender, AppServiceClosedEventArgs args) => Close(sender);

        private static void Close(AppServiceConnection instance)
        {
            if (Interlocked.CompareExchange(ref connection, null, instance) == instance)
            {
                instance.ServiceClosed -= OnClosed;
                instance.Dispose();
            }
        }
    }
}
