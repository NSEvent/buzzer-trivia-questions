#!/usr/bin/env node
/**
 * Validates all question JSON files in the questions/ directory.
 * Usage: node scripts/validate.js
 */
const fs = require('fs');
const path = require('path');

const questionsDir = path.join(__dirname, '..', 'questions');
const files = fs.readdirSync(questionsDir).filter(f => f.endsWith('.json'));

let errors = 0;

for (const file of files) {
  const filepath = path.join(questionsDir, file);
  const dateMatch = file.match(/^(\d{4}-\d{2}-\d{2})\.json$/);

  if (!dateMatch) {
    console.error(`❌ ${file}: Invalid filename format (expected YYYY-MM-DD.json)`);
    errors++;
    continue;
  }

  let data;
  try {
    data = JSON.parse(fs.readFileSync(filepath, 'utf8'));
  } catch (e) {
    console.error(`❌ ${file}: Invalid JSON — ${e.message}`);
    errors++;
    continue;
  }

  const date = dateMatch[1];
  const fileErrors = [];

  // Date match
  if (data.date !== date) {
    fileErrors.push(`date field "${data.date}" doesn't match filename "${date}"`);
  }

  // Categories
  if (!Array.isArray(data.categories) || data.categories.length !== 3) {
    fileErrors.push(`expected 3 categories, got ${data.categories?.length ?? 0}`);
  } else {
    for (let i = 0; i < 3; i++) {
      const cat = data.categories[i];
      if (!cat.name || typeof cat.name !== 'string') {
        fileErrors.push(`category ${i}: missing or invalid name`);
      }
      if (!Array.isArray(cat.questions) || cat.questions.length !== 5) {
        fileErrors.push(`category "${cat.name}": expected 5 questions, got ${cat.questions?.length ?? 0}`);
        continue;
      }
      const expectedValues = [200, 400, 600, 800, 1000];
      for (let j = 0; j < 5; j++) {
        const q = cat.questions[j];
        if (q.value !== expectedValues[j]) {
          fileErrors.push(`"${cat.name}" Q${j+1}: value should be ${expectedValues[j]}, got ${q.value}`);
        }
        validateQuestion(q, `"${cat.name}" Q${j+1}`, fileErrors);
      }
    }
  }

  // Daily Double
  if (!data.dailyDouble) {
    fileErrors.push('missing dailyDouble');
  } else {
    validateQuestion(data.dailyDouble, 'dailyDouble', fileErrors);
    if (!data.dailyDouble.category) {
      fileErrors.push('dailyDouble: missing category');
    }
  }

  // Bonus Round — required on Saturdays
  const dayOfWeek = new Date(date + 'T12:00:00').getDay(); // 6 = Saturday
  if (dayOfWeek === 6) {
    if (!data.bonusRound) {
      fileErrors.push('Saturday file must include bonusRound');
    } else {
      validateQuestion(data.bonusRound, 'bonusRound', fileErrors);
      if (!data.bonusRound.category) {
        fileErrors.push('bonusRound: missing category');
      }
    }
  }

  if (fileErrors.length > 0) {
    console.error(`❌ ${file}:`);
    fileErrors.forEach(e => console.error(`   - ${e}`));
    errors += fileErrors.length;
  } else {
    console.log(`✅ ${file}`);
  }
}

function validateQuestion(q, label, errors) {
  if (!q.clue || typeof q.clue !== 'string') {
    errors.push(`${label}: missing or invalid clue`);
  }
  if (!Array.isArray(q.choices) || q.choices.length !== 4) {
    errors.push(`${label}: expected 4 choices, got ${q.choices?.length ?? 0}`);
  } else {
    for (const choice of q.choices) {
      if (typeof choice !== 'string' || choice.length === 0) {
        errors.push(`${label}: empty or invalid choice`);
      }
    }
  }
  if (typeof q.correctIndex !== 'number' || q.correctIndex < 0 || q.correctIndex > 3) {
    errors.push(`${label}: correctIndex must be 0-3, got ${q.correctIndex}`);
  }
}

console.log(`\n${files.length} files checked, ${errors} error(s)`);
process.exit(errors > 0 ? 1 : 0);
