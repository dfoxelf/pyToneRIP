using System;
using System.Collections.Generic;

namespace pyToneRIP.Services
{
    internal static class CyclicBlendWeights
    {
        public static int[] Normalize(IEnumerable<int> source, int activeCount)
        {
            if (activeCount < 1 || activeCount > 3)
                throw new ArgumentOutOfRangeException(nameof(activeCount));
            var result = new[] { 0, 0, 0 };
            var index = 0;
            if (source != null)
            {
                foreach (var value in source)
                {
                    if (index >= activeCount) break;
                    result[index++] = Math.Max(0, Math.Min(100, value));
                }
            }
            if (activeCount == 1)
            {
                result[0] = 100;
                return result;
            }
            var total = 0;
            for (var item = 0; item < activeCount; item++) total += result[item];
            if (total == 0)
            {
                result[0] = 100;
                return result;
            }
            if (total < 100)
            {
                result[activeCount - 1] += 100 - total;
                return result;
            }
            var excess = total - 100;
            for (var offset = activeCount - 1; offset >= 0 && excess > 0; offset--)
            {
                var removed = Math.Min(result[offset], excess);
                result[offset] -= removed;
                excess -= removed;
            }
            return result;
        }

        public static int[] ApplyChange(
            IEnumerable<int> source,
            int changedIndex,
            int requestedValue,
            int activeCount)
        {
            if (changedIndex < 0 || changedIndex >= activeCount)
                throw new ArgumentOutOfRangeException(nameof(changedIndex));
            var result = Normalize(source, activeCount);
            var desired = Math.Max(0, Math.Min(100, requestedValue));
            var delta = desired - result[changedIndex];
            var remaining = Math.Abs(delta);
            for (var offset = 1; offset < activeCount && remaining > 0; offset++)
            {
                var target = (changedIndex + offset) % activeCount;
                if (delta > 0)
                {
                    var transfer = Math.Min(result[target], remaining);
                    result[target] -= transfer;
                    result[changedIndex] += transfer;
                    remaining -= transfer;
                }
                else if (delta < 0)
                {
                    var transfer = Math.Min(100 - result[target], remaining);
                    result[target] += transfer;
                    result[changedIndex] -= transfer;
                    remaining -= transfer;
                }
            }
            return result;
        }
    }
}
