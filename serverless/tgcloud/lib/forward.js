import { fetch } from 'sdk';
import { workerUrl, webhookSecret } from './config.js';

export default async function forward(payload, ctx) {
  const update = ctx?.update;
  if (!update || !Number.isSafeInteger(update.update_id)) {
    throw new Error('Full Telegram update context is required');
  }
  if (!workerUrl.startsWith('https://') || webhookSecret.length < 32) {
    throw new Error('Configure the private worker URL and webhook secret');
  }
  const response = await fetch(workerUrl.replace(/\/$/, '') + '/telegram/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Bot-Api-Secret-Token': webhookSecret },
    body: JSON.stringify(update),
  });
  // Throwing preserves Telegram webhook retries; never acknowledge an unpersisted update.
  if (!response.ok) throw new Error('Worker did not accept the update');
  return { accepted: true };
}

