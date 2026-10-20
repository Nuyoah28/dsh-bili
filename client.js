/** Local-library fork: retain DSH login/dependency/status UI, no autonomous or write actions. */
window.__ModuleLoader__.load({
  id: 'dsh-bili-scroll',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement
    const API = '/api/bili-duty'
    async function request(path, body) {
      const res = await fetch(API + path, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      if (!res.ok) throw new Error('请求失败（HTTP ' + res.status + '）')
      return res.json()
    }
    function Panel() {
      const [status, setStatus] = React.useState(null)
      const [login, setLogin] = React.useState(null)
      const [message, setMessage] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      React.useEffect(() => {
        let active = true
        const refresh = () => request('/status').then(v => { if (active) setStatus(v) })
          .catch(() => { if (active) setMessage('状态读取失败，请检查插件是否加载') })
        refresh()
        const timer = setInterval(refresh, 5000)
        return () => { active = false; clearInterval(timer) }
      }, [])
      async function action(path, body = {}) {
        setBusy(true)
        try {
          const value = await request(path, body)
          if (value.ok === false) throw new Error('操作失败，请检查环境和登录状态')
          if (path === '/login/start') setLogin(value)
          else if (path === '/login/poll') {
            setMessage(value.state === 'ok' ? '登录成功' : '扫码状态：' + value.state)
            if (value.state === 'ok' || value.state === 'expired') setLogin(null)
          } else setMessage('已提交操作，请查看环境状态')
        } catch (error) { setMessage(error.message) }
        finally { setBusy(false) }
      }
      const button = (label, path, body) => h('button', { disabled: busy, onClick: () => action(path, body) }, label)
      return h('div', { 'data-bili-library': '' },
        h('h3', null, 'B站资料库'),
        h('p', null, '由你提交链接，归档指定分P。没有每日推荐、自动评论、私信或收藏。'),
        h('p', null, status ? `登录：${status.loggedIn ? '已有凭证' : '未登录'}；原插件依赖：${status.venvReady ? '已安装' : '未安装'}` : '正在读取状态…'),
        h('div', null, button('安装原插件主依赖', '/install'), ' ', button('扫码登录', '/login/start')),
        login ? h('div', null,
          login.qr ? h('img', { src: login.qr, width: 160, height: 160, alt: 'B站登录二维码' })
            : h('p', null, '二维码未生成，请先安装主依赖后重试。'),
          button('扫码后检查登录', '/login/poll', { key: login.key })) : null,
        message ? h('p', { role: 'status' }, message) : null,
        h('p', null, '第一阶段使用本地命令行管理资料；原插件主依赖与资料库独立环境分别安装。'),
        h('pre', null, 'node tools/library.mjs setup\nnode tools/library.mjs doctor\nnode tools/library.mjs submit "视频链接?p=1"\nnode tools/library.mjs worker --drain\nnode tools/library.mjs favorites export 收藏夹ID'),
        h('p', null, '已有扫码凭证可直接复用，运行数据存放在插件家目录的 library 子目录。'))
    }
    return {
      inject: ['slots'],
      apply(ctx) {
        const style = document.createElement('style')
        style.setAttribute('data-plugin-css', 'dsh-bili-scroll')
        style.textContent = '[data-bili-library]{max-width:680px;line-height:1.6}[data-bili-library] button{margin:4px;padding:6px 12px}[data-bili-library] pre{white-space:pre-wrap}'
        document.head.appendChild(style)
        ctx.effect(() => () => style.remove(), 'bili-library: styles')
        const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
        slots?.inject('settings.section', () => slots.register(
          { name: 'settings.section', id: 'bili-scroll', order: 30, label: 'B站资料库' }, () => h(Panel)))
      },
    }
  },
})
