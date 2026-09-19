import { cli, Strategy } from '@jackwener/opencli/registry';
import { getPostDataJs, requireJikeIdentity } from './utils.js';
/**
 * 即刻首页动态流适配器
 *
 * 策略：导航到 web.okjike.com/following（需登录），
 * 通过 React fiber 树提取帖子数据。
 */
cli({
    site: 'jike',
    name: 'feed',
    access: 'read',
    description: '即刻首页动态流',
    domain: 'web.okjike.com',
    strategy: Strategy.COOKIE,
    browser: true,
    args: [
        { name: 'limit', type: 'int', default: 20 },
    ],
    columns: ['id', 'author', 'content', 'likes', 'comments', 'time', 'url'],
    func: async (page, kwargs) => {
        const limit = kwargs.limit || 20;
        // 1. 导航到即刻首页，等待 SPA 重定向到 /following
        await page.goto('https://web.okjike.com');
        await requireJikeIdentity(page);
        await page.wait({ selector: '[class*="_post_"]', timeout: 10 });

        // 2. 提取当前可见帖子（单次快照）
        const extractVisible = () => page.evaluate(`(() => {
        ${getPostDataJs}

        const results = [];
        const elements = document.querySelectorAll('[class*="_post_"]');

        for (const el of elements) {
          const data = getPostData(el);
          if (!data || !data.id) continue;

          const author = data.user?.screenName || data.target?.user?.screenName || '';
          const content = data.content || data.target?.content || '';
          if (!author && !content) continue;

          results.push({
            id: data.id,
            author,
            content: content.replace(/\\n/g, ' ').slice(0, 120),
            likes: data.likeCount || 0,
            comments: data.commentCount || 0,
            time: data.actionTime || data.createdAt || '',
            url: 'https://web.okjike.com/originalPost/' + data.id,
          });
        }

        return results;
      })()`);

        // 3. 增量收集：滚动 + 提取，合并去重（即刻使用虚拟滚动，DOM 只保留可见帖子）
        const allPosts = new Map();
        const mergePosts = (posts) => {
            for (const p of posts) {
                if (!allPosts.has(p.id)) allPosts.set(p.id, p);
            }
        };

        mergePosts(await extractVisible());

        const maxScrolls = Math.max(12, Math.ceil(limit / 2));
        let staleCount = 0;
        for (let i = 0; i < maxScrolls && allPosts.size < limit; i++) {
            const prevSize = allPosts.size;
            // 滚动一屏（即刻用 Mantine ScrollArea，滚动容器不是 window）
            await page.evaluate(`(() => {
                const viewport = document.querySelector('.mantine-ScrollArea-viewport');
                if (viewport) {
                    viewport.scrollBy(0, viewport.clientHeight);
                    viewport.dispatchEvent(new Event('scroll', { bubbles: true }));
                } else {
                    window.scrollTo(0, document.body.scrollHeight);
                }
            })()`);
            await page.wait(5);
            mergePosts(await extractVisible());
            // 连续五次无新内容则停止
            if (allPosts.size === prevSize) { staleCount++; if (staleCount >= 5) break; }
            else staleCount = 0;
        }

        return Array.from(allPosts.values()).slice(0, limit);
    },
});
