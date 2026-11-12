window.__ModuleLoader__.load({
  id: 'caregiving-agent-dsh',
  factory: (require) => {
    const { createElement } = require('react')

    function Icon() {
      return createElement('span', { style: { fontSize: 17, fontWeight: 700 } }, '守')
    }

    function MonitorPanel() {
      return createElement('iframe', {
        src: '/caregiving/',
        title: '守望者监控与配置',
        style: { display: 'block', width: '100%', height: '100%', border: 0 },
      })
    }

    function apply(ctx) {
      ctx.effect(() => ctx.slots.inject('main', () => ctx.slots.register({
        name: 'main', key: 'caregiving',
      }, MonitorPanel)))
      ctx.effect(() => ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
        name: 'sidebar.panellist', id: 'caregiving', order: 15, label: '守望者监控',
      }, Icon)))
    }

    return { name: 'caregiving-monitor-ui', inject: ['slots'], apply }
  },
})
