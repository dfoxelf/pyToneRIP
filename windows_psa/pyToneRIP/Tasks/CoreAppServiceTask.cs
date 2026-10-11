using System;
using System.Threading.Tasks;
using pyToneRIP.Shared;
using pyToneRIP.Tasks.Services;
using Windows.ApplicationModel.AppService;
using Windows.ApplicationModel.Background;
using Windows.Foundation.Collections;
using Windows.Storage;

namespace pyToneRIP.Tasks
{
    public sealed class CoreAppServiceTask : IBackgroundTask
    {
        private BackgroundTaskDeferral taskDeferral;

        public void Run(IBackgroundTaskInstance taskInstance)
        {
            taskDeferral = taskInstance.GetDeferral();
            taskInstance.Canceled += (_, __) => Complete();
            var details = taskInstance.TriggerDetails as AppServiceTriggerDetails;
            if (details == null)
            {
                Complete();
                return;
            }
            details.AppServiceConnection.RequestReceived += OnRequestReceived;
            details.AppServiceConnection.ServiceClosed += (_, __) => Complete();
        }

        private async void OnRequestReceived(
            AppServiceConnection sender,
            AppServiceRequestReceivedEventArgs args)
        {
            var requestDeferral = args.GetDeferral();
            try
            {
                var response = new ValueSet();
                try
                {
                    var request = args.Request.Message;
                    var jobId = request["job_id"] as string;
                    if ((request["schema"] as string) != "pytone-rip.core-request"
                        || !(request["version"] is int version) || version != 1
                        || (request["operation"] as string) != "render-job"
                        || !Guid.TryParse(jobId, out _))
                        throw new InvalidOperationException("Invalid pyToneRIP Core request.");

                    var sharedJobs = await SharedApplicationStorage.Root.GetFolderAsync("jobs");
                    var sharedJob = await sharedJobs.GetFolderAsync(jobId);
                    // Render directly in the publisher-shared job directory.
                    // A synchronous AppService request can be closed by Windows
                    // after about 90 seconds while a large RIP job legitimately
                    // continues. Keeping output in the shared folder lets the
                    // model workflow recover the terminal result without a
                    // fragile post-render copy from Manager LocalState.
                    var record = await sharedJob.GetFileAsync("job-record.json");
                    var result = await new RipRuntimeClient().ExecuteAsync(record);
                    response["success"] = true;
                    response["status"] = result.Status;
                }
                catch (OperationCanceledException error)
                {
                    response["success"] = false;
                    response["status"] = "CANCELLED";
                    response["error"] = error.Message;
                }
                catch (Exception error)
                {
                    response["success"] = false;
                    response["error"] = error.GetType().Name + ": " + error.Message;
                }
                await args.Request.SendResponseAsync(response);
            }
            finally
            {
                requestDeferral.Complete();
            }
        }

        private void Complete()
        {
            var current = taskDeferral;
            taskDeferral = null;
            current?.Complete();
        }
    }
}
