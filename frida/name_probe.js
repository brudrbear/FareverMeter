// name_probe.js — validate reading the skill display name from the live
// baseSkill.inf.texts.name virtual chain. DATA + OFF prepended by runner.
function log(m){send({kind:"log",msg:String(m)});}
function ptrPattern(a){const b=[];let v=uint64(a.toString());for(let i=0;i<8;i++){b.push(("0"+v.and(0xff).toNumber().toString(16)).slice(-2));v=v.shr(8);}return b.join(" ");}
function resolveAnchors(){const o=[],c={};for(const a of DATA.anchors){try{if(!(a.module in c))c[a.module]=Process.findModuleByName(a.module);const m=c[a.module];if(!m)continue;const ad=m.findExportByName(a.symbol);if(ad&&!ad.isNull())o.push({findex:a.findex,addr:ad});}catch(e){}}return o;}
function findTableBase(rs){const rr=Process.enumerateRanges("rw-").filter(r=>r.size<0x8000000);for(let s=0;s<Math.min(rs.length,6);s++){const seed=rs[s],pat=ptrPattern(seed.addr);for(const r of rr){let mm;try{mm=Memory.scanSync(r.base,r.size,pat);}catch(e){continue;}for(const m of mm){const base=m.address.sub(seed.findex*8);if(base.compare(r.base)<0)continue;let ag=0,ck=0;for(const o of rs){if(o===seed)continue;const sl=base.add(o.findex*8);if(sl.compare(r.base)<0||sl.add(8).compare(r.base.add(r.size))>0)continue;ck++;let v;try{v=sl.readPointer();}catch(e){continue;}if(v.equals(o.addr))ag++;if(ck>=8)break;}if(ag>=3)return base;}}}return null;}
function hlStr(p){try{if(!p||p.isNull())return null;const b=p.add(OFF.String.bytes).readPointer();if(b.isNull())return null;return b.readUtf16String();}catch(e){return null;}}

// Read vfield i of a vvirtual*: fields_data (@16) is void**; entry[i] points to
// the field's storage; deref once more for a pointer-typed field.
function vptr(vv, i){
    try{
        if(!vv||vv.isNull())return null;
        const fd=vv.add(16).readPointer();          // void** fields_data
        if(fd.isNull())return null;
        const slot=fd.add(i*8).readPointer();        // void* -> field storage
        if(!slot||slot.isNull())return null;
        return slot.readPointer();                   // pointer value (String*/vvirtual*)
    }catch(e){return null;}
}

let n=0; const MAX=20;
function main(){
    const base=findTableBase(resolveAnchors());
    if(!base){log("!! table not found");return;}
    const fi=DATA.count_targets["ent.Unit.onInflictDamage"];
    const addr=base.add(fi*8).readPointer();
    const INF=OFF.BaseSkill.inf,
          ID=OFF.SkillRow.id_vidx, TX=OFF.SkillRow.texts_vidx, NM=OFF.Texts.name_vidx;
    Interceptor.attach(addr,{onEnter(){
        if(n>=MAX)return;
        try{
            const dealer=this.context.rcx;
            const dr=this.context.rdx;
            const bs=dr.add(OFF.DamageResult.baseSkill).readPointer();
            if(!bs||bs.isNull())return;
            const kind=hlStr(bs.add(OFF.BaseSkill.kind).readPointer());
            const infVV=bs.add(INF).readPointer();     // vvirtual* skill row
            const idStr=hlStr(vptr(infVV, ID));         // should equal kind
            const textsVV=vptr(infVV, TX);              // vvirtual* texts
            const nameStr=hlStr(vptr(textsVV, NM));     // display name
            n++;
            log("#"+n+"  kind="+kind+"  inf.id="+idStr+"  name="+nameStr+
                (kind===idStr?"  [id OK]":"  [ID MISMATCH]"));
        }catch(e){log("err "+e);}
    }});
    log("hooked; DEAL DAMAGE (validating "+MAX+" names)");
}
main();
