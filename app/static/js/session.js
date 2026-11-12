// 会话状态（多轮）。只存在内存里：刷新页面就重新开始，避免"你以为在聊 A，
// 其实带着三天前的上下文"这种更难排查的问题。
//
// 回带 3 轮：够覆盖"它 / 那个字段 / 上面第二种情况"这类指代；
// 再多，改写提示词里会被无用的旧话题占满，反而改错。

export class Session {
  constructor(max = 3) {
    this.max = max;
    this.turns = [];
  }

  add(question, answer) {
    const q = (question || '').trim();
    const a = (answer || '').trim();
    if (!q || !a) return;
    // 拒答不进历史：把"证据不足"当成上一轮的答案喂回去，
    // 会让下一轮的改写以为"我们聊过这个但没答案"，行为变得很难解释。
    if (a.startsWith('证据不足')) return;
    this.turns.push({ q, a: a.slice(0, 400) });
    while (this.turns.length > this.max) this.turns.shift();
  }

  get size() { return this.turns.length; }

  reset() { this.turns = []; }

  payload() { return this.turns.map(t => ({ q: t.q, a: t.a })); }
}
