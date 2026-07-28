// localhero_probe.js — resolve GameApp.getMyHero, call it, read the local
// hero's name. Also hook onInflictDamage and show which hits are "mine".
// DATA + OFF prepended by runner.

function log(m) { send({ kind: "log", msg: m }); }
function ptrPattern(addr){const b=[];let v=uint64(addr.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const out=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())out.push({findex:a.findex,addr:ad});}catch(e){}}return out;}
function findTableBase(resolved){const ranges=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(resolved.length,6);s++){const seed=resolved[s],pat=ptrPattern(seed.addr);for(const r of ranges){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let agree=0,checked=0;for(const o of resolved){if(o===seed)continue;const slot=base.add(o.findex*8);if(slot.compare(r.base)<0||slot.add(8).compare(r.base.add(r.size))>0)continue;checked++;let v;try{v=slot.readPointer();}catch(e){continue;}if(v.equals(o.addr))agree++;if(checked>=8)break;}if(agree>=3)return base;}}}return null;}
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}
function typeName(p){try{if(!p||p.isNull())return null;const t=p.readPointer();const k=t.readU32();if(k===11||k===21)return t.add(8).readPointer().add(16).readPointer().readUtf16String();return "kind"+k;}catch(e){return null;}}

let localHero = null;
let mine = 0, other = 0;

function main() {
    const base = findTableBase(resolveAnchors());
    if (!base) { log("!! table not found"); return; }

    // Try each hero-accessor function; keep the first that returns an ent.Hero.
    for (const nm in DATA.funcs) {
        const fi = DATA.funcs[nm];
        let addr; try { addr = base.add(fi * 8).readPointer(); } catch (e) { continue; }
        try {
            const fn = new NativeFunction(addr, "pointer", []);
            const h = fn();
            const tn = typeName(h);
            const nmv = h && !h.isNull() ? hlStr(h.add(OFF.Hero.name).readPointer()) : null;
            log(nm + "() -> " + h + "  type=" + tn + "  name=" + nmv);
            if (tn === "ent.Hero" && !localHero) localHero = h;
        } catch (e) {
            log(nm + "() call failed: " + e);
        }
    }
    log("localHero = " + localHero);

    // Hook onInflictDamage; tag each hit mine/other vs localHero.
    const fi = DATA.count_targets["ent.Unit.onInflictDamage"];
    const daddr = base.add(fi * 8).readPointer();
    const DR = OFF.DamageResult, BS = OFF.BaseSkill, HERO = OFF.Hero;
    Interceptor.attach(daddr, {
        onEnter(a) {
            try {
                const hero = this.context.rcx, dr = this.context.rdx;
                const isMine = localHero && hero.equals(localHero);
                if (isMine) mine++; else other++;
                if ((isMine && mine <= 10) || (!isMine && other <= 3)) {
                    const amount = dr.add(DR._amount).readDouble();
                    const skill = hlStr(dr.add(DR.baseSkill).readPointer().add(BS.kind).readPointer());
                    const nm = hlStr(hero.add(HERO.name).readPointer());
                    log((isMine ? "[MINE] " : "[other]") + " " + (nm||"?") +
                        "  " + (skill||"?") + "  amt=" + amount.toFixed(1));
                }
            } catch (e) {}
        }
    });
    log(">>> DEAL DAMAGE — verifying local-hero filter <<<");
    setInterval(() => log("tally: mine=" + mine + " other=" + other), 5000);
}
main();
