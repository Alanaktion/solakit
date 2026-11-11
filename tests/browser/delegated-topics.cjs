/* Delegated navigation stays inside Details, including paginated refreshes. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {default:AxeBuilder} = require('@axe-core/playwright');
const base = 'http://127.0.0.1:18382';
(async () => {
  const browser=await chromium.launch({executablePath:process.env.SOLA_BROWSER_EXECUTABLE || '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',headless:true});
  const parent=`delegated-${Date.now()}`;
  try {
    for(const width of [1440,390]) {
      const context=await browser.newContext({viewport:{width,height:844}});
      const page=await context.newPage();
      page.setDefaultTimeout(10000);
      await page.goto(base+'/login');
      await page.locator('#token').fill('scratch-browser-token');
      await page.getByRole('button',{name:/Sign in/}).click();
      if(width===1440) {
        assert.equal((await page.request.post(base+'/api/sessions',{data:{id:parent}})).status(),201);
        for(let start=0;start<51;start+=10) {
          const responses=await Promise.all(Array.from({length:Math.min(10,51-start)},(_,i)=>page.request.post(base+'/api/sessions',{data:{id:`${parent}-${start+i}`,parent_session:parent}})));
          responses.forEach(response=>assert.equal(response.status(),201));
        }
      }
      await page.goto(`${base}/sessions/${parent}?child_offset=50`);
      assert.equal(await page.locator('#chat-sidebar .delegated-topics').count(),0);
      assert.equal(await page.locator('#chat-sidebar a[href*="'+parent+'-"]').count(),0);
      await page.getByRole('button',{name:'Details',exact:true}).click();
      await page.getByRole('link',{name:'Previous delegated topics'}).waitFor();
      const before=page.url();
      await page.getByRole('link',{name:'Previous delegated topics'}).click();
      await page.getByRole('link',{name:'Next delegated topics'}).waitFor();
      assert.equal(page.url(),before);
      assert.equal(await page.locator('#topic-details').isVisible(),true);
      assert.equal(await page.locator('.delegated-topics li').count(),50);
      const scan=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa','wcag22aa']).analyze();
      assert.deepEqual(scan.violations.map(item=>({rule:item.id,nodes:item.nodes.map(node=>node.target)})),[]);
      await page.locator('.delegated-topics li a').first().click();
      await page.getByRole('button',{name:'Details',exact:true}).click();
      await page.getByRole('link',{name:'Parent topic'}).click();
      assert.equal(new URL(page.url()).pathname,`/sessions/${parent}`);
      await context.close();
    }
    console.log('Delegated Details pagination, accessibility and parent navigation passed on desktop and mobile.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
