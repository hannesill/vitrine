'use strict';

// ================================================================
// FORM FIELDS — render, collect values, freeze confirmed state
// ================================================================
function renderForm(container, cardData) {
  var preview = cardData.preview;
  if (!preview || !preview.fields) return;
  renderFormFields(container, preview.fields);
}

function renderFormFields(container, fields) {
  var wrapper = document.createElement('div');
  wrapper.className = 'form-fields';

  fields.forEach(function(field) {
    var fieldEl = document.createElement('div');
    fieldEl.className = 'form-field';
    fieldEl.dataset.fieldName = field.name;
    fieldEl.dataset.fieldType = field.type;

    switch (field.type) {
      case 'question':
        renderQuestionField(fieldEl, field);
        break;
      default:
        fieldEl.textContent = 'Unknown field type: ' + field.type;
    }

    wrapper.appendChild(fieldEl);
  });

  container.appendChild(wrapper);
}

// A single choice among this many options or more reads better as a dropdown.
var DROPDOWN_MIN_OPTIONS = 7;

function renderQuestionField(container, field) {
  // Optional header chip
  if (field.header) {
    var chip = document.createElement('span');
    chip.className = 'question-header';
    chip.textContent = field.header;
    container.appendChild(chip);
  }

  // Question text
  var questionText = document.createElement('div');
  questionText.className = 'question-text';
  questionText.textContent = field.question || '';
  container.appendChild(questionText);

  if (!field.multiple && (field.options || []).length >= DROPDOWN_MIN_OPTIONS) {
    renderQuestionDropdown(container, field);
    return;
  }

  var optionsDiv = document.createElement('div');
  optionsDiv.className = 'question-options';

  var inputType = field.multiple ? 'checkbox' : 'radio';
  var groupName = 'question-' + field.name + '-' + Math.random().toString(36).slice(2, 8);
  var defaults = field.default;
  if (defaults != null && !Array.isArray(defaults)) defaults = [defaults];

  (field.options || []).forEach(function(opt) {
    var optLabel = document.createElement('label');
    optLabel.className = 'question-option';

    var input = document.createElement('input');
    input.type = inputType;
    if (inputType === 'radio') input.name = groupName;
    input.value = opt.label || '';
    input.dataset.fieldInput = field.name;

    if (defaults && defaults.indexOf(input.value) !== -1) {
      input.checked = true;
    }

    // Click on option card selects it
    input.addEventListener('change', function() {
      // Update selected state on option cards
      var siblings = optionsDiv.querySelectorAll('.question-option');
      siblings.forEach(function(sib) {
        var sibInput = sib.querySelector('input');
        sib.classList.toggle('selected', sibInput && sibInput.checked);
      });
      // Deselect "Other" when a pre-defined option is selected (single select)
      if (inputType === 'radio') {
        var otherOpt = optionsDiv.querySelector('.question-option-other');
        if (otherOpt) otherOpt.classList.remove('selected');
        var otherRadio = optionsDiv.querySelector('.question-option-other input[type="radio"]');
        if (otherRadio) otherRadio.checked = false;
      }
    });

    var labelSpan = document.createElement('span');
    labelSpan.className = 'question-option-label';
    labelSpan.textContent = opt.label || '';

    optLabel.appendChild(input);
    optLabel.appendChild(labelSpan);

    if (opt.description) {
      var descSpan = document.createElement('span');
      descSpan.className = 'question-option-desc';
      descSpan.textContent = opt.description;
      optLabel.appendChild(descSpan);
    }

    if (input.checked) optLabel.classList.add('selected');
    optionsDiv.appendChild(optLabel);
  });

  // "Other" option
  if (field.allow_other !== false) {
    var otherLabel = document.createElement('label');
    otherLabel.className = 'question-option question-option-other';

    var otherInput = document.createElement('input');
    otherInput.type = inputType;
    if (inputType === 'radio') otherInput.name = groupName;
    otherInput.value = '__other__';
    otherInput.dataset.fieldInput = field.name;

    var otherLabelSpan = document.createElement('span');
    otherLabelSpan.className = 'question-option-label';
    otherLabelSpan.textContent = 'Other:';

    var otherTextInput = document.createElement('input');
    otherTextInput.type = 'text';
    otherTextInput.className = 'question-other-input';
    otherTextInput.dataset.otherInput = field.name;
    otherTextInput.placeholder = 'Type your answer...';

    // Focus text input selects the "Other" radio/checkbox
    otherTextInput.addEventListener('focus', function() {
      otherInput.checked = true;
      otherLabel.classList.add('selected');
      if (inputType === 'radio') {
        var siblings = optionsDiv.querySelectorAll('.question-option:not(.question-option-other)');
        siblings.forEach(function(sib) {
          sib.classList.remove('selected');
          var sibInput = sib.querySelector('input');
          if (sibInput) sibInput.checked = false;
        });
      }
    });

    otherInput.addEventListener('change', function() {
      var siblings = optionsDiv.querySelectorAll('.question-option');
      siblings.forEach(function(sib) {
        var sibInput = sib.querySelector('input');
        sib.classList.toggle('selected', sibInput && sibInput.checked);
      });
    });

    otherLabel.appendChild(otherInput);
    otherLabel.appendChild(otherLabelSpan);
    otherLabel.appendChild(otherTextInput);
    optionsDiv.appendChild(otherLabel);
  }

  container.appendChild(optionsDiv);
}

function renderQuestionDropdown(container, field) {
  var row = document.createElement('div');
  row.className = 'question-dropdown';

  var select = document.createElement('select');
  select.className = 'question-select';
  select.dataset.fieldInput = field.name;
  select.setAttribute('aria-label', field.question || field.name);

  var placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Choose\u2026';
  select.appendChild(placeholder);

  var descriptions = {};
  (field.options || []).forEach(function(opt) {
    var option = document.createElement('option');
    option.value = opt.label || '';
    option.textContent = opt.label || '';
    if (opt.description) descriptions[option.value] = opt.description;
    select.appendChild(option);
  });

  var otherText = null;
  if (field.allow_other !== false) {
    var otherOption = document.createElement('option');
    otherOption.value = '__other__';
    otherOption.textContent = 'Other\u2026';
    select.appendChild(otherOption);
    otherText = document.createElement('input');
    otherText.type = 'text';
    otherText.className = 'question-other-input';
    otherText.dataset.otherInput = field.name;
    otherText.placeholder = 'Type your answer...';
    otherText.hidden = true;
  }

  var desc = document.createElement('div');
  desc.className = 'question-option-desc';

  function update() {
    desc.textContent = descriptions[select.value] || '';
    if (otherText) otherText.hidden = select.value !== '__other__';
  }
  select.addEventListener('change', update);

  var defaultValue = Array.isArray(field.default) ? field.default[0] : field.default;
  if (defaultValue != null) select.value = defaultValue;
  update();

  row.appendChild(select);
  if (otherText) row.appendChild(otherText);
  container.appendChild(row);
  container.appendChild(desc);
}

function collectFormValues(cardEl) {
  var values = {};
  var fields = cardEl.querySelectorAll('.form-field');
  fields.forEach(function(fieldEl) {
    var name = fieldEl.dataset.fieldName;
    var type = fieldEl.dataset.fieldType;

    switch (type) {
      case 'question': {
        var select = fieldEl.querySelector('select[data-field-input]');
        var hasCheckbox = fieldEl.querySelector('input[type="checkbox"]');
        if (select) {
          if (select.value === '__other__') {
            var otherValue = fieldEl.querySelector('[data-other-input]');
            values[name] = (otherValue && otherValue.value.trim()) ? otherValue.value.trim() : null;
          } else {
            values[name] = select.value || null;
          }
        } else if (hasCheckbox) {
          // Multi-select mode
          var selected = [];
          var checked = fieldEl.querySelectorAll('input[type="checkbox"]:checked');
          checked.forEach(function(cb) {
            if (cb.value === '__other__') {
              var otherText = fieldEl.querySelector('[data-other-input]');
              if (otherText && otherText.value.trim()) selected.push(otherText.value.trim());
            } else {
              selected.push(cb.value);
            }
          });
          values[name] = selected;
        } else {
          // Single-select mode
          var sel = fieldEl.querySelector('input[type="radio"]:checked');
          if (sel && sel.value === '__other__') {
            var otherText = fieldEl.querySelector('[data-other-input]');
            values[name] = (otherText && otherText.value.trim()) ? otherText.value.trim() : null;
          } else {
            values[name] = sel ? sel.value : null;
          }
        }
        break;
      }
    }
  });
  return values;
}

function _resolveOptionDesc(fieldName, selected, fields) {
  // Find the field spec and look up the option description for a selected label
  var field = null;
  (fields || []).forEach(function(f) {
    if (f.name === fieldName) field = f;
  });
  if (!field || !field.options) return '';
  var match = '';
  field.options.forEach(function(opt) {
    if (opt && opt.label === selected && opt.description) {
      match = opt.description;
    }
  });
  return match;
}

function renderFrozenForm(container, values, fields) {
  var frozen = document.createElement('div');
  frozen.className = 'form-frozen';

  // Build a map of field names to labels
  var labelMap = {};
  (fields || []).forEach(function(f) {
    labelMap[f.name] = f.header || f.label || f.question || f.name;
  });

  Object.keys(values).forEach(function(key) {
    var val = values[key];
    var item = document.createElement('span');
    item.className = 'form-frozen-item';

    var labelSpan = document.createElement('span');
    labelSpan.className = 'frozen-label';
    labelSpan.textContent = (labelMap[key] || key) + ':';
    item.appendChild(labelSpan);

    var valueSpan = document.createElement('span');
    valueSpan.className = 'frozen-value';
    if (typeof val === 'boolean') {
      valueSpan.textContent = val ? 'yes' : 'no';
    } else if (Array.isArray(val)) {
      valueSpan.textContent = val.join(' \u2013 ');
    } else {
      valueSpan.textContent = String(val);
    }
    item.appendChild(valueSpan);

    // Look up and display option descriptions
    var descTexts = [];
    if (Array.isArray(val)) {
      val.forEach(function(v) {
        var d = _resolveOptionDesc(key, v, fields);
        if (d) descTexts.push(d);
      });
    } else if (typeof val === 'string') {
      var d = _resolveOptionDesc(key, val, fields);
      if (d) descTexts.push(d);
    }
    if (descTexts.length > 0) {
      var descSpan = document.createElement('small');
      descSpan.className = 'frozen-desc';
      descSpan.textContent = descTexts.join(' · ');
      item.appendChild(descSpan);
    }

    frozen.appendChild(item);
  });

  container.appendChild(frozen);
}
