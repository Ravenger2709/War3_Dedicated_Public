// SPDX-License-Identifier: Apache-2.0
#include "src/hostbot/native_g1_dual_probe.h"
#include <cassert>
#include <cstring>
#include <initializer_list>
#include <iostream>

namespace {
using namespace war3g1;
const uint32_t P0=7, P1=19, Epoch=37;
const uint32_t A0=832, B0=833, A1=945, B1=946;

Bytes cache(const std::string &key,uint32_t value,
            const std::string &name="AIG1.w3v",const std::string &mission="g1") {
 Bytes v(1,0x6b);
 for(const std::string &s:{name,mission,key}) {
  v.insert(v.end(),s.begin(),s.end());v.push_back(0);
 }
 put32(v,value);return v;
}
Bytes chat(const std::string &text,uint32_t a=A0,uint32_t b=B0) {
 Bytes v(1,0x60);put32(v,a);put32(v,b);
 v.insert(v.end(),text.begin(),text.end());v.push_back(0);return v;
}
Bytes batch(std::initializer_list<Bytes> parts) {
 Bytes v;for(const Bytes &part:parts)v.insert(v.end(),part.begin(),part.end());return v;
}
Bytes exports(uint32_t raw0=1,uint32_t raw1=0,uint32_t accepted=1) {
 return batch({cache("raw0",raw0),cache("raw1",raw1),cache("accepted_probe",accepted)});
}
Bytes probe(uint32_t epoch,uint32_t tick,uint32_t a,uint32_t b) {
 return chat("~g1:"+std::to_string(epoch)+":0:"+std::to_string(tick)+":0",a,b);
}
void no_output(const DualProbeResult &r) {
 assert(!r.queued && !r.peer_ack && !r.confirmed);
 assert(r.downlink0.empty() && r.downlink1.empty());
}
void quiet(const DualProbeResult &r) {
 assert(r.allowed && !r.failed);no_output(r);
}
void failed(const DualProbeResult &r) {
 assert(r.allowed && r.failed && !r.diagnostic);no_output(r);
}

struct Fixture {
 DualProbeSession session;
 uint32_t epoch;
 explicit Fixture(uint32_t e=Epoch):epoch(e) {session.configure(A0,B0,A1,B1,e);assert(session.enabled());}
 DualProbeResult send(unsigned colour,const Bytes &v,bool running=true) {
  return from(colour==0?P0:P1,colour,v,running);
 }
 DualProbeResult from(uint32_t pid,unsigned colour,const Bytes &v,bool running=true) {
  DualProbeResult r=session.process(pid,colour,v,running);
  // Every emitted action, including on negative paths, must be one seq=0,
  // kind=0 probe. An actor command is forbidden even after confirmation.
  const Bytes *out[]={&r.downlink0,&r.downlink1};
  for(unsigned i=0;i<2;++i)if(!out[i]->empty()) {
   std::vector<Event> events;assert(decode(*out[i],events));assert(events.size()==1);
   const Event &e=events[0];assert(e.opcode==0x60);
   assert(e.a==(i==0?A0:A1) && e.b==(i==0?B0:B1));
   const std::string prefix="~g1:"+std::to_string(epoch)+":0:";
   assert(e.text.compare(0,prefix.size(),prefix)==0);
   assert(e.text.size()>prefix.size()+2);
   assert(e.text.substr(e.text.size()-2)==":0");
   uint32_t tick=0;
   assert(decimal(e.text.substr(prefix.size(),e.text.size()-prefix.size()-2),tick));
   assert(tick>=8 && r.queued && !r.failed);
  }
  assert(r.downlink0.empty()==r.downlink1.empty());
  assert(r.queued==!r.downlink0.empty());
  return r;
 }
 void claim(unsigned colour) {quiet(send(colour,cache("wire_version",3)));}
 void claims() {claim(0);claim(1);}
 void queued(const DualProbeResult &r,uint32_t tick) {
  assert(r.allowed && r.queued && !r.failed && !r.peer_ack && !r.confirmed);
  assert(r.pid0==P0 && r.pid1==P1);
  assert(r.downlink0==probe(epoch,tick,A0,B0));
  assert(r.downlink1==probe(epoch,tick,A1,B1));
 }
 void queue(uint32_t t0=8,uint32_t t1=8) {
  claims();quiet(send(0,cache("tick",t0)));
  queued(send(1,cache("tick",t1)),t0>t1?t0:t1);
 }
 DualProbeResult ack(unsigned colour,uint32_t tick=9,uint32_t raw0=1,uint32_t raw1=0) {
  return send(colour,batch({cache("tick",tick),exports(raw0,raw1),cache("probe",epoch)}));
 }
 void retired() {
  for(unsigned c=0;c<2;++c) {
   quiet(send(c,batch({cache("wire_version",3),cache("tick",100),exports(),cache("probe",epoch)})));
  }
 }
};

void configuration() {
 DualProbeSession s;assert(!s.enabled());
 quiet(s.process(P0,0,chat("~g1:disabled"),true));
 for(unsigned slot=0;slot<4;++slot)for(uint32_t bad:{0U,65536U,0xffffffffU}) {
  uint32_t pairs[]={A0,B0,A1,B1};pairs[slot]=bad;
  s.configure(pairs[0],pairs[1],pairs[2],pairs[3],Epoch);assert(!s.enabled());
 }
 for(uint32_t bad:{0U,1U<<28,0xffffffffU}) {
  s.configure(A0,B0,A1,B1,bad);assert(!s.enabled());
 }
 s.configure(1,65535,65535,1,(1U<<28)-1);assert(s.enabled());
}
void both_claims_and_ticks() {
 for(unsigned first=0;first<2;++first) {
  Fixture f;f.claims();quiet(f.send(first,cache("tick",8)));
  quiet(f.send(1-first,cache("tick",7)));
  f.queued(f.send(1-first,cache("tick",8)),8);
 }
 // An unclaimed peer's clock is not retained as authority.
 Fixture f;f.claim(0);quiet(f.send(0,cache("tick",8)));
 quiet(f.send(1,cache("tick",8)));f.claim(1);
 f.queued(f.send(1,cache("tick",8)),8);
}
void one_peer_never_queues() {
 for(unsigned c=0;c<2;++c) {
  Fixture f;f.claim(c);
  for(uint32_t tick:{8U,9U,28U,100U}) {
   quiet(f.send(c,batch({cache("tick",tick),exports(),cache("probe",Epoch)})));
  }
 }
}
void maximum_tick_and_targets() {
 for(unsigned ahead=0;ahead<2;++ahead) {
  Fixture f;f.queue(ahead==0?12:8,ahead==1?12:8);
  // Both peers compare against the common source tick, not their own start.
  quiet(f.ack(1-ahead,9));
  DualProbeResult a=f.ack(1-ahead,13);assert(a.peer_ack && !a.confirmed);
  DualProbeResult b=f.ack(ahead,13);assert(b.peer_ack && b.confirmed);
 }
}
void wrong_version_and_namespace() {
 for(unsigned c=0;c<2;++c)for(uint32_t version:{0U,1U,2U,4U,0xffffffffU}) {
  Fixture f;f.claim(1-c);quiet(f.send(1-c,cache("tick",8)));
  quiet(f.send(c,batch({cache("wire_version",version),cache("tick",8)})));
  f.claim(c);f.queued(f.send(c,cache("tick",8)),8);
 }
 for(unsigned c=0;c<2;++c)for(unsigned variant=0;variant<4;++variant) {
  const std::string name=variant==0?"aig1.w3v":variant==1?"AIG1.w3v.extra":"AIG1.w3v";
  const std::string mission=variant==2?"G1":variant==3?"g1.extra":"g1";
  Fixture f;f.claim(1-c);quiet(f.send(1-c,cache("tick",8)));
  quiet(f.send(c,batch({cache("wire_version",3,name,mission),cache("tick",8,name,mission)})));
  quiet(f.send(c,cache("tick",8))); // Wrong namespace cannot bind a PID.
  f.claim(c);quiet(f.send(c,cache("tick",8,name,mission)));
  f.queued(f.send(c,cache("tick",8)),8);
 }
}
void invalid_pid_and_colour() {
 Fixture f;
 for(uint32_t pid:{0U,255U,256U,0xffffffffU})for(unsigned c=0;c<2;++c)
  quiet(f.from(pid,c,batch({cache("wire_version",3),cache("tick",8)})));
 for(unsigned c:{2U,11U,255U})
  quiet(f.from(31,c,batch({cache("wire_version",3),cache("tick",8)})));
 f.queue();
}
void metadata_identity_spoof() {
 for(unsigned c=0;c<2;++c) {
  Fixture duplicate;duplicate.claim(c);
  failed(duplicate.from(c==0?P0:P1,1-c,cache("wire_version",3)));
  duplicate.retired();
  Fixture replacement;replacement.claim(c);
  failed(replacement.from(31,c,cache("wire_version",3)));
  replacement.retired();
 }
}
void two_peer_confirmation() {
 for(unsigned first=0;first<2;++first)for(unsigned raw=0;raw<3;++raw) {
  Fixture f;f.queue();
  DualProbeResult a=f.ack(first,9,raw==1?0:1,raw==0?0:1);
  assert(a.allowed && a.peer_ack && !a.confirmed && !a.failed && a.diagnostic);
  assert(a.raw0==(raw==1?0U:1U) && a.raw1==(raw==0?0U:1U) && a.accepted==1);
  no_output(f.send(1-first,Bytes()));
  DualProbeResult b=f.ack(1-first);assert(b.peer_ack && b.confirmed && !b.failed);
 }
}
void early_ack() {
 for(unsigned c=0;c<2;++c) {
  Fixture f;f.queue();quiet(f.send(c,batch({exports(),cache("probe",Epoch)})));
  DualProbeResult a=f.ack(c);assert(a.peer_ack && !a.confirmed);
 }
 // ACK in the queueing batch cannot borrow a clock that appears after it.
 Fixture f;f.claims();quiet(f.send(0,cache("tick",8)));
 f.queued(f.send(1,batch({cache("tick",8),exports(),cache("probe",Epoch),cache("tick",9)})),8);
 assert(f.send(1,cache("probe",Epoch)).peer_ack);
}
void wrong_and_nonparticipant_ack() {
 for(unsigned c=0;c<2;++c) {
  Fixture f;f.queue();quiet(f.send(c,batch({cache("tick",9),exports()})));
  for(uint32_t wrong:{0U,Epoch-1,Epoch+1,0xffffffffU})quiet(f.send(c,cache("probe",wrong)));
  for(unsigned colour=0;colour<2;++colour)
   quiet(f.from(31,colour,batch({cache("tick",100),exports(),cache("probe",Epoch)})));
  quiet(f.from(c==0?P0:P1,1-c,batch({cache("tick",100),exports(),cache("probe",Epoch)})));
  assert(f.send(c,cache("probe",Epoch)).peer_ack);
 }
}
void stale_prequeue_exports() {
 for(unsigned c=0;c<2;++c) {
  Fixture f;f.claims();quiet(f.send(c,exports()));
  quiet(f.send(0,cache("tick",8)));f.queued(f.send(1,cache("tick",8)),8);
  failed(f.send(c,batch({cache("tick",9),cache("probe",Epoch)})));f.retired();
 }
}
void incomplete_exports() {
 for(unsigned c=0;c<2;++c)for(unsigned missing=0;missing<3;++missing) {
  Fixture f;f.queue();quiet(f.send(c,cache("tick",9)));
  const char *keys[]={"raw0","raw1","accepted_probe"};
  for(unsigned i=0;i<3;++i)if(i!=missing)quiet(f.send(c,cache(keys[i],i==1?0:1)));
  failed(f.send(c,cache("probe",Epoch)));f.retired();
 }
 // A second peer cannot reuse the first peer's fresh diagnostics.
 Fixture f;f.queue();assert(f.ack(0).peer_ack);
 failed(f.send(1,batch({cache("tick",9),cache("probe",Epoch)})));
}
void wrong_export_namespace() {
 for(unsigned variant=0;variant<3;++variant) {
  Fixture f;f.queue();quiet(f.send(0,cache("tick",9)));
  for(const char *key:{"raw0","raw1","accepted_probe"}) {
   Bytes v=cache(key,std::strcmp(key,"raw1")==0?0:1,
                 variant==0?"other.w3v":"AIG1.w3v",variant==1?"other":"g1");
   if(variant==2)v[0]=0x6c; // Same fields in another action are not integer exports.
   quiet(f.send(0,v));
  }
  failed(f.send(0,cache("probe",Epoch)));
 }
}
void invalid_export_values() {
 for(unsigned c=0;c<2;++c)for(unsigned variant=0;variant<2;++variant) {
  Fixture f;f.queue();
  failed(f.send(c,batch({cache("tick",9),variant==0?exports(0,0,1):exports(1,0,0),cache("probe",Epoch)})));
  f.retired();
 }
}
void duplicate_ack_and_no_actors() {
 Fixture f;f.queue();assert(f.ack(0).peer_ack);
 for(unsigned i=0;i<4;++i)quiet(f.send(0,cache("probe",Epoch)));
 DualProbeResult r=f.ack(1);assert(r.peer_ack && r.confirmed);
 for(unsigned c=0;c<2;++c) {
  quiet(f.send(c,batch({exports(),cache("probe",Epoch),cache("probe",Epoch)})));
  for(uint32_t tick:{10U,16U,28U,100U})quiet(f.send(c,cache("tick",tick)));
 }
}
void invalid_counter_after_ack() {
 for(unsigned c=0;c<2;++c)for(unsigned other_acked=0;other_acked<2;++other_acked)
  for(const char *key:{"raw0","raw1","accepted_probe"}) {
   Fixture f;f.queue();if(other_acked)assert(f.ack(1-c).peer_ack);
   // A later invalid export must erase the earlier diagnostic, ACK, and even
   // two-peer confirmation from this result before native code can log them.
   failed(f.send(c,batch({cache("tick",9),exports(),cache("probe",Epoch),cache(key,2)})));
   f.retired();
  }
}
void double_accepted_and_raw_overflow() {
 for(unsigned c=0;c<2;++c)for(const char *key:{"raw0","raw1","accepted_probe"})
  for(uint32_t value:{2U,0xffffffffU}) {
   Fixture f;f.queue();failed(f.send(c,cache(key,value)));f.retired();
  }
 // Queueing and discovering a duplicate delivery in one call must clear both packets.
 for(const char *key:{"raw0","raw1","accepted_probe"}) {
  Fixture f;f.claims();quiet(f.send(0,cache("tick",8)));
  failed(f.send(1,batch({cache("tick",8),cache(key,2)})));f.retired();
 }
}
void deadline_boundary() {
 for(unsigned c=0;c<2;++c) {
  Fixture ontime;ontime.queue();assert(ontime.ack(c,28).peer_ack);
  Fixture late;late.queue();failed(late.ack(c,29));late.retired();
 }
}
void whole_batch_deadline() {
 for(unsigned c=0;c<2;++c)for(unsigned reverse=0;reverse<2;++reverse)
  for(unsigned other_acked=0;other_acked<2;++other_acked) {
   Fixture f;f.queue();if(other_acked)assert(f.ack(1-c).peer_ack);
   quiet(f.send(c,batch({cache("tick",9),exports()})));
   failed(f.send(c,reverse?batch({cache("tick",29),cache("probe",Epoch)}):
                                  batch({cache("probe",Epoch),cache("tick",29)})));
   f.retired();
  }
}
void queue_batch_deadline() {
 // The fence also applies when this same batch first queues the two probes.
 Fixture f;f.claims();quiet(f.send(0,cache("tick",8)));
 failed(f.send(1,batch({cache("tick",8),cache("tick",9),exports(),cache("probe",Epoch),cache("tick",29)})));
 f.retired();
}
void running_false_freezes() {
 Fixture f;
 for(unsigned c=0;c<2;++c)
  quiet(f.send(c,batch({cache("wire_version",3),cache("tick",8),exports()}),false));
 f.claims();quiet(f.send(0,cache("tick",8)));f.queued(f.send(1,cache("tick",8)),8);
 for(unsigned c=0;c<2;++c) {
  quiet(f.send(c,batch({cache("tick",100),exports(2,2,2),cache("probe",Epoch)}),false));
  quiet(f.send(c,cache("wire_version",3),false));
 }
 // Frozen exports cannot authorize an ACK after simulation resumes.
 quiet(f.send(0,exports(),false));quiet(f.send(0,cache("tick",9)));
 failed(f.send(0,cache("probe",Epoch)));
 Fixture resumed;resumed.queue();quiet(resumed.send(0,cache("tick",100),false));
 assert(resumed.ack(0).peer_ack);assert(resumed.ack(1).confirmed);
}
void departure() {
 for(unsigned c=0;c<2;++c)for(unsigned phase=0;phase<3;++phase) {
  Fixture f;
  if(phase==0)f.claims();else f.queue();
  if(phase==2)assert(f.ack(c).peer_ack);
  f.session.depart(c==0?P0:P1);f.retired();
 }
 Fixture unrelated;unrelated.queue();unrelated.session.depart(31);
 assert(unrelated.ack(0).peer_ack);assert(unrelated.ack(1).confirmed);
}
void epoch_and_session_isolation() {
 Fixture old;old.queue();assert(old.ack(0).peer_ack);
 Fixture fresh(Epoch+1);fresh.queue();
 for(unsigned c=0;c<2;++c) {
  quiet(fresh.send(c,batch({cache("tick",9),exports(),cache("probe",Epoch)})));
  quiet(old.send(c,cache("probe",Epoch+1)));
 }
 assert(fresh.send(0,cache("probe",Epoch+1)).peer_ack);
 assert(fresh.send(1,cache("probe",Epoch+1)).confirmed);
 assert(old.ack(1).confirmed);
 old.session.configure(A0,B0,A1,B1,Epoch); // Reconfigure discards claims, ACKs and counters.
 quiet(old.send(0,batch({cache("tick",9),exports(),cache("probe",Epoch)})));
 old.queue();assert(old.ack(0).peer_ack);assert(old.ack(1).confirmed);
}
void malformed_and_reserved() {
 for(bool running:{false,true}) {
  Fixture f;
  for(const Bytes &bad:{Bytes(1028,0x01),Bytes{0xff},chat("~g1:private-test-marker"),Bytes{0x06,'x'}}) {
   DualProbeResult r=f.send(0,batch({cache("wire_version",3),cache("tick",8),bad}),running);
   assert(!r.allowed && !r.failed && !r.diagnostic);no_output(r);
  }
  // Rejected batches must not have retained their valid metadata prefix.
  f.claim(1);quiet(f.send(1,cache("tick",8)));quiet(f.send(0,cache("tick",8)));
  f.claim(0);f.queued(f.send(0,cache("tick",8)),8);
  assert(!f.send(1,chat("~g1:37:0:8:0"),running).allowed);
  assert(f.ack(0).peer_ack);assert(f.ack(1).confirmed);
 }
 // A reserved marker in an ordinary order payload is not a chat action.
 Fixture f;Bytes order(23,0);order[0]=0x11;
 const std::string marker="~g1:";std::copy(marker.begin(),marker.end(),order.begin()+2);
 quiet(f.send(0,order));
}
void truncation() {
 for(const Bytes &whole:{cache("wire_version",3),chat("g1:hello")})
  for(size_t n=1;n<whole.size();++n) {
   Fixture f;Bytes cut(whole.begin(),whole.begin()+n);
   DualProbeResult r=f.send(0,cut);assert(!r.allowed);no_output(r);
   f.queue();
  }
}
void ordinary_pause_and_save() {
 Fixture f;
 const Bytes save={0x06,'t','e','s','t',0};
 for(unsigned c=0;c<2;++c) {
  quiet(f.send(c,Bytes{0x01}));quiet(f.send(c,Bytes{0x02}));quiet(f.send(c,save));
  quiet(f.send(c,batch({Bytes{0x01},save,Bytes{0x02},cache("wire_version",3)})));
 }
 quiet(f.send(0,batch({save,cache("tick",8)})));
 f.queued(f.send(1,batch({Bytes{0x01},cache("tick",8),Bytes{0x02}})),8);
 assert(f.ack(0).peer_ack);assert(f.ack(1).confirmed);
 quiet(f.send(0,chat("ordinary private-test-marker")));
}

struct Test {const char *name;void (*run)();};
const Test tests[]={
 {"configuration",configuration},
 {"both_claims_and_ticks",both_claims_and_ticks},
 {"one_peer_never_queues",one_peer_never_queues},
 {"maximum_tick_and_targets",maximum_tick_and_targets},
 {"wrong_version_and_namespace",wrong_version_and_namespace},
 {"invalid_pid_and_colour",invalid_pid_and_colour},
 {"metadata_identity_spoof",metadata_identity_spoof},
 {"two_peer_confirmation",two_peer_confirmation},
 {"early_ack",early_ack},
 {"wrong_and_nonparticipant_ack",wrong_and_nonparticipant_ack},
 {"stale_prequeue_exports",stale_prequeue_exports},
 {"incomplete_exports",incomplete_exports},
 {"wrong_export_namespace",wrong_export_namespace},
 {"invalid_export_values",invalid_export_values},
 {"duplicate_ack_and_no_actors",duplicate_ack_and_no_actors},
 {"invalid_counter_after_ack",invalid_counter_after_ack},
 {"double_accepted_and_raw_overflow",double_accepted_and_raw_overflow},
 {"deadline_boundary",deadline_boundary},
 {"whole_batch_deadline",whole_batch_deadline},
 {"queue_batch_deadline",queue_batch_deadline},
 {"running_false_freezes",running_false_freezes},
 {"departure",departure},
 {"epoch_and_session_isolation",epoch_and_session_isolation},
 {"malformed_and_reserved",malformed_and_reserved},
 {"truncation",truncation},
 {"ordinary_pause_and_save",ordinary_pause_and_save}
};
}

int main(int argc,char **argv) {
 // Optional selection permits reporting all independent assert failures in CI.
 if(argc==2 && std::strcmp(argv[1],"--list")==0) {
  for(const Test &test:tests)std::cout<<test.name<<'\n';return 0;
 }
 unsigned count=0;
 for(const Test &test:tests)if(argc==1 || (argc==2 && std::strcmp(argv[1],test.name)==0)) {
  test.run();++count;std::cout<<"PASS "<<test.name<<'\n';
 }
 assert(count!=0);std::cout<<"G1 dual-probe: "<<count<<" tests PASS\n";
}
