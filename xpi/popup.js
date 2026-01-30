document.getElementById("extractBtn").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const statusDiv = document.getElementById("status");

  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      function: extractFullTrace,
    });
    
    const data = results[0].result;
    
    if (data) {
      await navigator.clipboard.writeText(JSON.stringify(data));
      statusDiv.innerText = "✅ 提取成功 (已兼容多语言)!";
      statusDiv.style.color = "green";
    } else {
      statusDiv.innerText = "❌ 未找到数据";
      statusDiv.style.color = "red";
    }
  } catch (err) {
    statusDiv.innerText = "❌ 错误: " + err.message;
    console.error(err);
  }
});

function extractFullTrace() {
  // --- 1. Search Metadata ---
  function findMetadata(labelText) {
    const labels = Array.from(document.querySelectorAll('.text-secondary span'));
    const targetLabel = labels.find(el => el.innerText.trim() === labelText);
    if (targetLabel) {
      const row = targetLabel.closest('.flex.w-full.justify-between');
      if (row && row.lastElementChild) {
        return row.lastElementChild.innerText.trim();
      }
    }
    return null;
  }

  // --- 2. Date Parsing ---
  function parseDateToTimestamp(dateStr) {
    if (!dateStr) return Date.now();

    // Step A: Try parsing English/ISO format
    let timestamp = Date.parse(dateStr);
    
    if (!isNaN(timestamp)) {
      return timestamp;
    }

    // Step B: Try parsing Chinese format
    const standardized = dateStr
      .replace(/年/g, '/')
      .replace(/月/g, '/')
      .replace(/日/g, '')
      .trim();
    
    timestamp = Date.parse(standardized);

    // Step C: Fallback to current time
    if (isNaN(timestamp)) {
      console.warn("Date parse failed for:", dateStr, "Using current time.");
      return Date.now();
    }

    return timestamp;
  }

  // --- 3. Content Extraction ---
  function extractContentFromRow(row) {
    const markdownBlock = row.querySelector('.g1ul0');
    if (markdownBlock) {
      return markdownBlock.innerText.trim();
    }

    const headerDiv = row.querySelector('.flex.flex-row.items-center');
    if (headerDiv && headerDiv.nextElementSibling) {
      return headerDiv.nextElementSibling.innerText.trim();
    }
    
    return row.innerText.trim();
  }

  // ================= Main Logic =================
  const trace = {
    timestamp: 0,
    session_id: "",
    model: "", 
    input: {
      messages: [] 
    },
    output: "" 
  };

  // 1. Metadata
  const createdStr = findMetadata("Created");
  trace.timestamp = parseDateToTimestamp(createdStr); // 使用新的智能解析函数
  trace.session_id = findMetadata("ID") || "";
  trace.model = findMetadata("Model") || "unknown";

  // 2. Container 
  const containers = document.querySelectorAll('.nyCLx');

  // --- Input ---
  if (containers.length > 0) {
    const inputContainer = containers[0];
    const messageRows = Array.from(inputContainer.children).filter(div => 
        div.classList.contains('flex') && div.classList.contains('flex-col')
    );

    messageRows.forEach(row => {
      const roleSpan = row.querySelector('.font-semibold');
      if (!roleSpan) return;
      
      const role = roleSpan.innerText.trim().toLowerCase();
      const content = extractContentFromRow(row);

      if (content) {
        trace.input.messages.push({ role, content });
      }
    });
  }

  // --- Output ---
  if (containers.length > 1) {
    const outputContainer = containers[1];
    const outputRow = outputContainer.querySelector('.flex.flex-col');
    
    if (outputRow) {
      const content = extractContentFromRow(outputRow);
      if (content) trace.output = content;
    } else {
      trace.output = outputContainer.innerText.trim();
    }
  }

  return trace;
}