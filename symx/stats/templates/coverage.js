document.addEventListener("DOMContentLoaded", function () {
  const dataElement = document.getElementById("coverage-data");
  if (dataElement === null) {
    return;
  }

  const data = parseCoverageData(dataElement.textContent || "{}");
  initCoverageSection("ipsw", Array.isArray(data.ipswRows) ? data.ipswRows : []);
  initCoverageSection("ota", Array.isArray(data.otaRows) ? data.otaRows : []);
});

function parseCoverageData(text) {
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

function initCoverageSection(sectionId, rows) {
  const controls = {
    platform: loadFilterControl(sectionId, "platform"),
    major: loadFilterControl(sectionId, "major"),
    minor: loadFilterControl(sectionId, "minor"),
    patch: loadFilterControl(sectionId, "patch"),
  };
  const tbody = document.getElementById(sectionId + "-tbody");
  const summary = document.getElementById(sectionId + "-summary");
  const resetButton = document.getElementById(sectionId + "-reset");

  if (
    controls.platform === null ||
    controls.major === null ||
    controls.minor === null ||
    controls.patch === null ||
    tbody === null ||
    summary === null ||
    resetButton === null
  ) {
    return;
  }

  const orderedRows = rows.slice();
  const totalCount = sumCounts(orderedRows);

  controls.platform.select.addEventListener("change", function () {
    clearFilters(controls.major, controls.minor, controls.patch);
    refresh();
  });
  controls.major.select.addEventListener("change", function () {
    clearFilters(controls.minor, controls.patch);
    refresh();
  });
  controls.minor.select.addEventListener("change", function () {
    clearFilters(controls.patch);
    refresh();
  });
  controls.patch.select.addEventListener("change", refresh);
  resetButton.addEventListener("click", function () {
    clearFilters(controls.platform, controls.major, controls.minor, controls.patch);
    refresh();
  });

  refresh();

  function refresh() {
    updateSelect(
      controls.platform,
      uniqueValuesInOrder(orderedRows, "platform"),
      "All platforms",
      { hideIfEmpty: false }
    );

    let filters = currentFilters(controls);
    updateSelect(
      controls.major,
      uniqueValuesInOrder(filterRowsForMajorOptions(orderedRows, filters), "major"),
      "All major versions",
      { hideIfEmpty: true }
    );

    filters = currentFilters(controls);
    updateSelect(
      controls.minor,
      uniqueValuesInOrder(filterRowsForMinorOptions(orderedRows, filters), "minor"),
      "All minor versions",
      { hideIfEmpty: true }
    );

    filters = currentFilters(controls);
    updateSelect(
      controls.patch,
      uniqueValuesInOrder(filterRowsForPatchOptions(orderedRows, filters), "patch"),
      "All patch versions",
      { hideIfEmpty: true }
    );

    filters = currentFilters(controls);
    const filteredRows = orderedRows.filter(function (row) {
      return matchesFilters(row, filters);
    });

    renderTableBody(tbody, filteredRows);
    renderSummary(summary, sumCounts(filteredRows), totalCount);
    resetButton.disabled = !hasActiveFilters(filters);
  }
}

function loadFilterControl(sectionId, name) {
  const wrapper = document.getElementById(sectionId + "-" + name + "-control");
  const select = document.getElementById(sectionId + "-" + name);
  if (wrapper === null || select === null) {
    return null;
  }

  return { wrapper, select };
}

function clearFilters() {
  Array.from(arguments).forEach(function (control) {
    control.select.value = "";
  });
}

function currentFilters(controls) {
  return {
    platform: controls.platform.select.value,
    major: controls.major.select.value,
    minor: controls.minor.select.value,
    patch: controls.patch.select.value,
  };
}

function filterRowsForMajorOptions(rows, filters) {
  return rows.filter(function (row) {
    return filters.platform === "" || row.platform === filters.platform;
  });
}

function filterRowsForMinorOptions(rows, filters) {
  return rows.filter(function (row) {
    return (
      (filters.platform === "" || row.platform === filters.platform) &&
      (filters.major === "" || String(row.major) === filters.major)
    );
  });
}

function filterRowsForPatchOptions(rows, filters) {
  return rows.filter(function (row) {
    return (
      (filters.platform === "" || row.platform === filters.platform) &&
      (filters.major === "" || String(row.major) === filters.major) &&
      (filters.minor === "" || String(row.minor) === filters.minor)
    );
  });
}

function matchesFilters(row, filters) {
  return (
    (filters.platform === "" || row.platform === filters.platform) &&
    (filters.major === "" || String(row.major) === filters.major) &&
    (filters.minor === "" || String(row.minor) === filters.minor) &&
    (filters.patch === "" || String(row.patch) === filters.patch)
  );
}

function updateSelect(control, values, allLabel, options) {
  const current = control.select.value;
  control.select.textContent = "";

  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = allLabel;
  control.select.appendChild(allOption);

  const validValues = new Set([""]);
  values.forEach(function (value) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(value);
    control.select.appendChild(option);
    validValues.add(String(value));
  });

  control.select.value = validValues.has(current) ? current : "";
  control.select.disabled = values.length === 0;
  control.wrapper.hidden = options.hideIfEmpty && values.length === 0;
}

function uniqueValuesInOrder(rows, fieldName) {
  const values = [];
  const seen = new Set();
  rows.forEach(function (row) {
    const value = row[fieldName];
    if ((typeof value !== "string" && typeof value !== "number") || seen.has(value)) {
      return;
    }
    seen.add(value);
    values.push(value);
  });
  return values;
}

function renderTableBody(tbody, rows) {
  tbody.textContent = "";
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.textContent = "No rows.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  rows.forEach(function (row) {
    const tr = document.createElement("tr");

    const platformCell = document.createElement("td");
    platformCell.textContent = row.platform;
    tr.appendChild(platformCell);

    const versionCell = document.createElement("td");
    versionCell.textContent = row.versionDisplay;
    tr.appendChild(versionCell);

    const countCell = document.createElement("td");
    countCell.className = "count";
    countCell.textContent = String(row.count);
    tr.appendChild(countCell);

    tbody.appendChild(tr);
  });
}

function renderSummary(summary, visibleCount, totalCount) {
  summary.textContent = "Count: ";

  const strong = document.createElement("strong");
  strong.textContent = String(visibleCount);
  summary.appendChild(strong);

  if (visibleCount !== totalCount) {
    summary.append(" of " + String(totalCount));
  }
}

function hasActiveFilters(filters) {
  return filters.platform !== "" || filters.major !== "" || filters.minor !== "" || filters.patch !== "";
}

function sumCounts(rows) {
  return rows.reduce(function (sum, row) {
    return sum + row.count;
  }, 0);
}
