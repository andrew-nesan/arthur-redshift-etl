function apiCall(path, handler) {
    var xhttp = new XMLHttpRequest();
    xhttp.onreadystatechange = function () {
        if (this.readyState === 4) {
            // console.log(path + ": " + this.statusText);
            if (this.status === 200) {
                handler(JSON.parse(this.responseText));
            } else {
                console.log("There was an error retrieving data from " + path);
            }
        }
    };
    xhttp.open("GET", path, true);
    xhttp.send();
}

function fetchEtlId() {
    apiCall("/api/etl-id", function setEtlId(obj) {
        document.getElementById("etl-id").innerHTML = obj.id
    });
}

function fetchEtlIndices() {
    apiCall("/api/indices", updateEtlIndices);
}

function updateEtlIndices(etlIndices) {
    // Update table with the current progress meter (200px * percentage = 2 * percentage points)
    var table = "<tr><th>Name</th><th>Current Index</th><th>Final Index</th><th colspan='2'>Progress</th></tr>";
    var len = etlIndices.length;
    var done = 0;
    if (len === 0) {
       table += "<tr><td colspan='5'>(waiting...)</td></tr>";
    } /* else */
    for (var i = 0; i < len; i++) {
        var e = etlIndices[i];
        if (e.current === e.final) {
            done += 1
        }
        var percentage = (100.0 * e.current) / e.final;
        var percentageLabel = (percentage > 10.0) ? percentage.toFixed(0) : percentage.toFixed(1);
        table += "<tr>" +
            "<td>" + e.name + "</td>" +
            "<td>" + e.current + "</td>" +
            "<td>" + e.final + "</td>" +
            "<td>" + percentageLabel + "% </td>" +
            "<td class='progress'><div style='width:" + (2 * percentage).toFixed(2) + "px'></div></td>" +
            "</tr>";
    }
    document.getElementById("indices-table").innerHTML = table;
    if (done < 1 || done < len) {
        setTimeout(fetchEtlIndices, 1000);
    }
}

function fetchEtlEvents() {
    apiCall("/api/events", updateEtlEvents);
}

function updateEtlEvents(etlEvents) {
    var table = "<tr><th>Target</th><th>Step</th><th>Last Event</th><th>Timestamp</th><th>Elapsed</th></tr>";
    var len = etlEvents.length;
    if (len === 0) {
        table += "<tr><td colspan='6'>(waiting...)</td></tr>";
    } /* else */
    for (var i = 0; i < len; i++) {
        var e = etlEvents[i];
        var elapsed = e.elapsed || 0.0; // should be: timestamp - now
        var elapsedLabel = (elapsed > 10.0) ? elapsed.toFixed(1) : elapsed.toFixed(2);
        table += "<tr>" +
            "<td>" + e.target + "</td>" +
            "<td>" + e.step + "</td>" +
            "<td>" + e.event + "</td>" +
            "<td>" + e.timestamp.substring(0, 19) + "</td>" +
            "<td>" + elapsedLabel + "</td>" +
            "</tr>";
    }
    document.getElementById("events-table").innerHTML = table;
    setTimeout(fetchEtlEvents, 1000);
}

window.onload = function () {
    fetchEtlId();
    fetchEtlEvents();
    fetchEtlIndices();
};
