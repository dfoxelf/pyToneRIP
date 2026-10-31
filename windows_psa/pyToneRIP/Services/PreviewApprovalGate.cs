using System;
using System.Threading.Tasks;

namespace pyToneRIP.Shared
{
    internal static class PreviewApprovalGate
    {
        public static async Task WaitAsync(Func<bool> approved, Func<bool> canceled, Task uiClosed, Func<Task> poll = null)
        {
            while (true)
            {
                if (canceled()) throw new OperationCanceledException("Print preview was canceled.");
                if (poll != null) await poll();
                if (canceled()) throw new OperationCanceledException("Print preview was canceled.");
                if (approved()) return;
                if (uiClosed.IsCompleted)
                {
                    await uiClosed;
                    throw new OperationCanceledException("Print preview closed before approval.");
                }
                await Task.WhenAny(uiClosed, Task.Delay(100));
            }
        }
    }
}
