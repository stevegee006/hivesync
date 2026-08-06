/* Number formatting shared by the activity strip and the run progress panel.
 *
 * One implementation on purpose. Both panels report the same kinds of figure,
 * and a second copy is how two things with the same user-facing shape drift
 * apart, which has already happened here more than once.
 *
 * Loaded without `defer` because the inline scripts that use it run during
 * parse. A deferred script would not exist yet when they do.
 */
window.HiveSync = (function () {
  var UNITS = ["B", "KB", "MB", "GB", "TB"];

  function bytes(value) {
    var index = 0;
    var amount = Number(value) || 0;
    while (amount >= 1024 && index < UNITS.length - 1) {
      amount /= 1024;
      index++;
    }
    return (index === 0 ? amount.toFixed(0) : amount.toFixed(1)) + " " + UNITS[index];
  }

  function rate(value) {
    return bytes(value) + "/s";
  }

  /* rclone reports an ETA in whole seconds, and reports null until it has
   * enough history to estimate one. Null is rendered as nothing at all rather
   * than as a zero, because a confident "0s" on a transfer that has barely
   * started is worse than an empty space. */
  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return total + "s";
    var minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + "m " + (total % 60) + "s";
    var hours = Math.floor(minutes / 60);
    return hours + "h " + (minutes % 60) + "m";
  }

  return { bytes: bytes, rate: rate, duration: duration };
})();
