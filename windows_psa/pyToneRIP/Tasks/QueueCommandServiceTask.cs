using System;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Services;
using Windows.ApplicationModel;
using Windows.ApplicationModel.AppService;
using Windows.ApplicationModel.Background;
using Windows.Foundation.Collections;

namespace pyToneRIP.Tasks
{
    public sealed class QueueCommandServiceTask : IBackgroundTask
    {
        private static readonly object Gate = new object();
        private static readonly SemaphoreSlim HelperStart = new SemaphoreSlim(1);
        private static AppServiceConnection helper;
        private static TaskCompletionSource<AppServiceConnection> pendingHelper;
        private static int clients;
        private BackgroundTaskDeferral deferral;
        private AppServiceConnection connection;
        private bool isHelper;

        public void Run(IBackgroundTaskInstance taskInstance)
        {
            deferral = taskInstance.GetDeferral();
            var details = taskInstance.TriggerDetails as AppServiceTriggerDetails;
            if (details == null || details.CallerPackageFamilyName != Package.Current.Id.FamilyName)
            {
                Interlocked.Exchange(ref deferral, null)?.Complete();
                return;
            }
            connection = details.AppServiceConnection;
            isHelper = details.Name == QueueAppServiceProtocol.HelperService;
            lock (Gate)
            {
                if (isHelper)
                {
                    if (pendingHelper == null || helper != null || clients == 0)
                    {
                        Interlocked.Exchange(ref deferral, null)?.Complete();
                        return;
                    }
                    helper = connection;
                }
                else
                {
                    clients++;
                    connection.RequestReceived += OnRequestReceived;
                }
                connection.ServiceClosed += OnServiceClosed;
                taskInstance.Canceled += OnCanceled;
                if (isHelper)
                    pendingHelper.TrySetResult(connection);
            }
        }

        private async void OnRequestReceived(AppServiceConnection sender, AppServiceRequestReceivedEventArgs args)
        {
            var requestDeferral = args.GetDeferral();
            try
            {
                ValueSet response;
                try
                {
                    var target = await GetHelperAsync();
                    if (target == null)
                        response = new ValueSet { [QueueAppServiceProtocol.NotDispatched] = true };
                    else
                    {
                        var reply = await target.SendMessageAsync(args.Request.Message);
                        if (reply.Status == AppServiceResponseStatus.Success && reply.Message != null)
                            response = reply.Message;
                        else if (reply.Status == AppServiceResponseStatus.MessageSizeTooLarge)
                            response = new ValueSet { [QueueAppServiceProtocol.ReplyTooLarge] = true };
                        else
                            response = new ValueSet { [QueueAppServiceProtocol.Error] =
                                "QueueHelper did not return a result: " + reply.Status };
                    }
                }
                catch (Exception error)
                {
                    response = new ValueSet { [QueueAppServiceProtocol.Error] =
                        "QueueHelper AppService failed: " + error.Message };
                }
                await args.Request.SendResponseAsync(response);
            }
            catch (Exception)
            {
                // The client can disconnect while the helper finishes the request.
            }
            finally
            {
                requestDeferral.Complete();
            }
        }

        private static async Task<AppServiceConnection> GetHelperAsync()
        {
            await HelperStart.WaitAsync();
            TaskCompletionSource<AppServiceConnection> pending = null;
            try
            {
                lock (Gate)
                {
                    if (helper != null)
                        return helper;
                    if (clients == 0)
                        return null;
                    pendingHelper = pending = new TaskCompletionSource<AppServiceConnection>(
                        TaskCreationOptions.RunContinuationsAsynchronously);
                }
                try
                {
                    await FullTrustProcessLauncher.LaunchFullTrustProcessForCurrentAppAsync(QueueAppServiceProtocol.LaunchGroup);
                }
                catch (Exception)
                {
                    return null;
                }
                await Task.WhenAny(pending.Task, Task.Delay(TimeSpan.FromSeconds(5)));
                lock (Gate)
                    return helper;
            }
            finally
            {
                lock (Gate)
                {
                    if (pendingHelper == pending)
                        pendingHelper = null;
                }
                HelperStart.Release();
            }
        }

        private void OnServiceClosed(AppServiceConnection sender, AppServiceClosedEventArgs args) => Complete();
        private void OnCanceled(IBackgroundTaskInstance sender, BackgroundTaskCancellationReason reason) => Complete();

        private void Complete()
        {
            var active = Interlocked.Exchange(ref deferral, null);
            if (active == null)
                return;
            AppServiceConnection closeHelper = null;
            lock (Gate)
            {
                connection.ServiceClosed -= OnServiceClosed;
                if (isHelper)
                {
                    if (helper == connection) helper = null;
                }
                else
                {
                    connection.RequestReceived -= OnRequestReceived;
                    if (--clients == 0)
                    {
                        closeHelper = helper;
                        helper = null;
                        pendingHelper?.TrySetResult(null);
                    }
                }
            }
            closeHelper?.Dispose();
            active.Complete();
        }
    }
}
