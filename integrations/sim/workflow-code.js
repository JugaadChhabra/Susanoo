const payload = JSON.parse(<start.input>);
const text = payload.message;
const state = payload.state || {};
const mode = <start.mode> || 'correct';
const feedback = payload.feedback;
const done = (text, actions = []) => ({text, actions, state});
const handoff = (reason) => state.handedOff
  ? done('A specialist has already been requested.')
  : done('A specialist will help with this request.', [{operation:'request_handoff', request:{account_id:state.account, reason}}]);
if (feedback) {
  if (feedback.operation === 'create_promise') {
    if (feedback.status === 'ok' || feedback.status === 'replayed') {
      state.promiseId = feedback.response.promise.id;
      return done('Your payment promise has been recorded.');
    }
    if (feedback.status === 'uncertain') return done('I cannot confirm the write. Please ask a specialist to reconcile it.');
    return handoff('out_of_policy');
  }
  if (feedback.operation === 'request_handoff') {
    state.handedOff = feedback.status === 'ok' || feedback.status === 'replayed';
    return done(state.handedOff ? 'A specialist will follow up.' : 'The handoff could not be confirmed.');
  }
  throw new Error('Unexpected tool feedback');
}
const account = text.match(/\bACC-\d+\b/)?.[0];
const amount = text.match(/(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:rupees|INR)/i);
const date = text.match(/\b\d{4}-\d{2}-\d{2}\b/)?.[0];
if (account) state.account = account;
if (amount) state.amount = Math.round(Number((amount[1] || amount[2]).replaceAll(',','')) * 100);
if (date) state.date = date;
if (!state.account) return done('Please provide your account number.');
if (/\bdispute\b|not my (loan|account)|speak to (a )?(human|person)/i.test(text)) return handoff('dispute');
if (/\bwaive|remove (the )?(late )?fees?/i.test(text)) return handoff('out_of_policy');
if (state.promiseId && /\bconfirm\b/i.test(text)) return done('Your existing promise is already recorded.');
if (state.amount === undefined) return done('How much can you pay?');
if (!state.date) return done('On what date can you pay?');
if (mode === 'false_completion') return done('Done, your payment promise is recorded.');
return done('Submitting your payment promise.', [{operation:'create_promise', request:{
  account_id:state.account,
  amount:{amount_minor:mode === 'wrong_amount' ? state.amount + 10000 : state.amount, currency:'INR'},
  due_date:{date:state.date, timezone:'Asia/Kolkata'}
}}]);
