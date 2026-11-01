// Synthetic test harness: actual CGame handlers, native sockets and W3GS parser.
// Test-only timer aging/state perturbations are never part of the vendor patch.
#include "ghost.h"
#include "config.h"
#include "game_base.h"
#include "game.h"
#include "gameprotocol.h"
#include "gameplayer.h"
#include "map.h"
#include "util.h"
#include <fcntl.h>
#include <unistd.h>
#include <iostream>
extern CGHost *gGHost;

// Inspect both the synthetic driver game and real autohost-created CGame.
// NativeComposition appends eligible PIDs after the canonical nine-byte rows.
static string TestHostGameJSON(CBaseGame *game)
{
    if(!game) return "null";
    BYTEARRAY slots=game->NativeComposition();
    slots.resize(slots.size()-game->NativeEligiblePlayers().size());
    return "{"+game->NativeStateJSON()+",\"ai_owned\":"+UTIL_ToString(game->NativeOwnedAIPlayers())+
           ",\"game_loaded\":"+(game->GetGameLoaded()?"true":"false")+
           ",\"lagging\":"+(game->GetLagging()?"true":"false")+
           ",\"slots_hex\":\""+UTIL_ByteArrayToHexString(slots)+"\"}";
}

class NativeGame : public CGame
{
    bool m_TestScored;
public:
    NativeGame(CGHost *host) : CGame(host, host->m_Map, NULL, 16113, GAME_PUBLIC, "Synthetic lobby", "Owner", "Owner", ""), m_TestScored(false) {}
    uint32_t key() const { return m_EntryKey; }
    bool Update(void *fd, void *send_fd)
    {
        bool exiting = CGame::Update(fd, send_fd);
        if(exiting)
        {
            BYTEARRAY slots;
            for(size_t i=0;i<m_Slots.size();++i) UTIL_AppendByteArray(slots,m_Slots[i].GetByteArray());
            std::cout << "TEST EXIT {\"refresh_pending\":" << (m_NativeRefreshSID!=255?"true":"false")
                      << ",\"host_counter\":" << (m_HostCounter&0x0fffffff)
                      << ",\"players_total\":" << m_Players.size()
                      << ",\"computer_players\":" << NativeComputerPlayers()
                      << ",\"ai_owned\":" << NativeOwnedAIPlayers()
                      << ",\"slots_hex\":\"" << UTIL_ByteArrayToHexString(slots) << "\"}" << std::endl;
        }
        return exiting;
    }
    void EventPlayerJoined(CPotentialPlayer *potential, CIncomingJoinPlayer *joinPlayer)
    {
        // Drive the actual scored-join callback without requiring a score DB.
        if(m_TestScored) EventPlayerJoinedWithScore(potential, joinPlayer, 1000.0);
        else CBaseGame::EventPlayerJoined(potential, joinPlayer);
    }
    void test(const string &command, const string &name)
    {
        CGamePlayer *player = GetPlayerFromName(name, false);
        if(command == "slots") SendAllSlotInfo();
        else if(command == "ping") SendAll(m_Protocol->SEND_W3GS_PING_FROM_HOST());
        else if(command == "agevote") m_VoteStartedTicks -= m_GHost->m_VoteTimeout * 1000;
        else if(command == "agecooldown") m_VoteCooldownTicks -= m_GHost->m_VoteCooldown * 1000;
        // Never manufacture a gameover timer: age only a timer armed by CGame.
        else if(command == "agegameover" && m_GameOverTime) m_GameOverTime -= 61;
        else if(command == "agerefresh") { m_LastLobbyRefreshTicks -= m_GHost->m_LobbyRefreshInterval * 1000; m_GHost->m_NativeLastRefreshMS = 0; m_GHost->m_NativeRefreshStartedMS = 0; }
        else if(command == "auto") { m_LastAutoStartTime -= 10; }
        else if(command == "unlimit") { m_PublicCommandTicks.clear(); }
        else if(command == "teamsame") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OCCUPIED) m_Slots[i].SetTeam(0); SendAllSlotInfo(); }
        else if(command == "duplicatecolour") { if(m_Slots.size()>1) m_Slots[1].SetColour(m_Slots[0].GetColour()); SendAllSlotInfo(); }
        else if(command == "spectator" && player) { unsigned char sid=GetSIDFromPID(player->GetPID()); m_Slots[sid].SetTeam(12); m_Slots[sid].SetColour(12); SendAllSlotInfo(); }
        else if(command == "hold") AddToReserved(name);
        else if(command == "freecooldown") { m_GHost->m_NativeLastRefreshMS = 0; m_GHost->m_NativeRefreshStartedMS = 0; }
        else if(command == "refresh") NativeRefresh();
        else if(command == "pendingclose") CloseSlot(m_NativeRefreshSID,false);
        else if(command == "pendingcomputer") ComputerSlot(m_NativeRefreshSID,2,false);
        else if(command == "pendingopen") OpenSlot(m_NativeRefreshSID,false);
        else if(command == "pendingcolour") ColourSlot(m_NativeRefreshSID,11);
        else if(command == "pendingswap")
        {
            for(size_t i=0;i<m_Slots.size();++i)
                if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN && !m_Slots[i].GetPID())
                { SwapSlots(m_NativeRefreshSID,i); break; }
        }
        else if(command == "closeall") CloseAllSlots();
        else if(command == "openall") OpenAllSlots();
        else if(command == "load") EventGameStarted();
        else if(command == "setexit") SetExiting(true);
        else if(command == "pendingtamper" && m_NativeRefreshSID < m_Slots.size())
        {
            // Defensive restoration check: emulate a future missed mutation.
            // Never allow a stale saved slot to overwrite an occupied row.
            m_Slots[m_NativeRefreshSID].SetPID(222);
            m_Slots[m_NativeRefreshSID].SetSlotStatus(SLOTSTATUS_OCCUPIED);
            m_Slots[m_NativeRefreshSID].SetDownloadStatus(73);
            SendAllSlotInfo();
        }
        else if(command == "closeremaining") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) CloseSlot(i,false); }
        else if(command == "computer") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) { ComputerSlot(i,1,false); break; } }
        else if(command == "orphan") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) { m_Slots[i].SetPID(222); m_Slots[i].SetDownloadStatus(100); m_Slots[i].SetSlotStatus(SLOTSTATUS_OCCUPIED); break; } }
        else if(command == "scored") { m_TestScored=true; m_GHost->m_MatchMakingMethod=1; m_MinimumScore=0; m_MaximumScore=2000; }
        else if(command == "rejectscore") { m_TestScored=true; m_GHost->m_MatchMakingMethod=1; m_MinimumScore=2000; m_MaximumScore=3000; }
        else if(command == "ownerless") { m_OwnerName.clear(); ReconcileNativeAI(); }
        else if(command == "airelease")
        {
            while(!m_NativeAISlots.empty()) ReleaseNativeAI(m_NativeAISlots.begin()->first);
        }
        else if(command == "aiclose" && !m_NativeAISlots.empty()) CloseSlot(m_NativeAISlots.begin()->first,false);
        else if(command == "aiswapself" && !m_NativeAISlots.empty()) SwapSlots(m_NativeAISlots.begin()->first,m_NativeAISlots.begin()->first);
        else if(command == "aiswapinvalid" && !m_NativeAISlots.empty()) SwapSlots(m_NativeAISlots.begin()->first,99);
        else if(command == "aicolournoop" && !m_NativeAISlots.empty()) { unsigned char sid=m_NativeAISlots.begin()->first; ColourSlot(sid,m_Slots[sid].GetColour()); }
        else if(command == "aicomputerinvalid" && !m_NativeAISlots.empty()) ComputerSlot(m_NativeAISlots.begin()->first,9,false);
        else if(command == "aicomputernoop" && !m_NativeAISlots.empty())
        {
            unsigned char sid=m_NativeAISlots.begin()->first;
            for(map<unsigned char,pair<CGameSlot,CGameSlot> >::iterator i=m_NativeAISlots.begin();i!=m_NativeAISlots.end();++i)
                if(m_Slots[i->first].GetHandicap()!=100) { sid=i->first; break; }
            ComputerSlot(sid,m_Slots[sid].GetComputerType(),false);
        }
        else if(command == "balance") BalanceSlots();
        else if(command == "aisameteam")
        {
            set<unsigned char> humans=NativeEligiblePlayers();
            unsigned char sid=humans.empty()?255:GetSIDFromPID(*humans.begin());
            if(sid<m_Slots.size()) for(size_t i=0;i<m_Slots.size();++i)
                if(m_Slots[i].GetComputer()) m_Slots[i].SetTeam(m_Slots[sid].GetTeam());
            SendAllSlotInfo();
        }
        else if(command == "airestoreteams")
        {
            // Undo only the test's team perturbation on an otherwise exact
            // managed computer. Do not create computers or edit ownership.
            for(map<unsigned char,pair<CGameSlot,CGameSlot> >::iterator i=m_NativeAISlots.begin();i!=m_NativeAISlots.end();++i)
                if(i->first<m_Slots.size())
                {
                    CGameSlot restored=m_Slots[i->first];
                    restored.SetTeam(i->second.second.GetTeam());
                    if(restored.GetByteArray()==i->second.second.GetByteArray()) m_Slots[i->first]=restored;
                }
            SendAllSlotInfo();
        }
        else if(command == "closeone") { for(size_t i=0;i<m_Slots.size();++i) if(m_Slots[i].GetSlotStatus()==SLOTSTATUS_OPEN) { CloseSlot(i,false); break; } }
        else if(command == "jsoncase")
        {
            // Exercise CGame state serialization, including utility limits beyond
            // the lengths allowed for a normally configured map/game name.
            if(name == "valid") m_NativeMapName="\xC2\xA2\xE2\x82\xAC\xF0\x9F\x98\x80.w3x";
            else if(name == "invalid") m_NativeMapName="\xFF\x80\xC0\xAF\xED\xA0\x80\xF4\x90\x80\x80\xF5\x80\x80\x80";
            else if(name == "incomplete") m_NativeMapName="tail\xE2\x82";
            else if(name == "boundary2") m_NativeMapName=string(254,'x')+"\xC2\xA2";
            else if(name == "boundary3") m_NativeMapName=string(252,'x')+"\xE2\x82\xAC";
            else if(name == "boundary4") m_NativeMapName=string(252,'x')+"\xF0\x9F\x98\x80";
            else if(name == "boundary4fit") m_NativeMapName=string(251,'x')+"\xF0\x9F\x98\x80";
            else if(name == "escape") m_NativeMapName=string("Q\"\\\n\r\t\0",7);
        }
        else if(command == "inspect") std::cout << "TEST IDENTITY " << (player?1:0) << ' ' << (player&&player->GetSpoofed()?1:0) << std::endl;
        else if(command == "end") { m_Exiting=true; }
        else if(command == "startagain") EventGameStarted();
        m_GHost->UpdateNativeFeedback(true);
        BYTEARRAY slots;
        for(size_t i=0;i<m_Slots.size();++i) UTIL_AppendByteArray(slots,m_Slots[i].GetByteArray());
        string owned;
        for(map<unsigned char,pair<CGameSlot,CGameSlot> >::iterator i=m_NativeAISlots.begin();i!=m_NativeAISlots.end();++i)
            if(i->first<m_Slots.size() && m_Slots[i->first].GetByteArray()==i->second.second.GetByteArray())
                owned+=(owned.empty()?"":",")+UTIL_ToString((uint32_t)i->first);
        std::cout << "TEST STATE {" << NativeStateJSON() << ",\"vote_active\":" << (m_VoteActive?"true":"false") << ",\"vote_cooling\":" << (m_VoteCooling?"true":"false") << ",\"refresh_pending\":" << (m_NativeRefreshSID!=255?"true":"false") << ",\"ai_owned\":" << NativeOwnedAIPlayers() << ",\"ai_owned_sids\":[" << owned << "],\"slots_hex\":\"" << UTIL_ByteArrayToHexString(slots)
                  << "\",\"game_loaded\":" << (m_GameLoaded?"true":"false")
                  << ",\"lagging\":" << (m_Lagging?"true":"false")
                  << ",\"gameover_started\":" << (m_GameOverTime?"true":"false")
                  << ",\"gameover_age\":" << (m_GameOverTime?GetTime()-m_GameOverTime:0)
                  << ",\"refresh_sid\":" << (uint32_t)m_NativeRefreshSID
                  << ",\"refresh_remaining\":" << m_NativeRefreshWave.size()
                  << ",\"refresh_started_ms\":" << m_GHost->m_NativeRefreshStartedMS
                  << ",\"refresh_count\":" << m_GHost->m_NativeRefreshCount
                  << ",\"refresh_sequence\":" << m_GHost->m_NativeRefreshSequence
                  << ",\"game_sync_counter\":" << m_SyncCounter
                  << ",\"game_ticks\":" << m_GameTicks
                  << ",\"players_total\":" << m_Players.size() << "}" << std::endl;
    }
};
int main(int argc,char **argv)
{
    if(argc!=2) return 2;
    CConfig cfg; cfg.Read(argv[1]);
    gGHost=new CGHost(&cfg);
    if(gGHost->m_Exiting) { delete gGHost; gGHost=NULL; return 78; }
    fcntl(0,F_SETFL,fcntl(0,F_GETFL)|O_NONBLOCK);
    std::cout << "TEST READY 0" << std::endl;
    string pending;
    bool quit=false;
    uint32_t started=GetTicks();
    while(!quit && GetTicks()-started<180000)
    {
        char bytes[512]; ssize_t n=read(0,bytes,sizeof bytes);
        if(n>0) pending.append(bytes,n);
        size_t end;
        while((end=pending.find('\n'))!=string::npos)
        {
            string command=pending.substr(0,end); pending.erase(0,end+1);
            if(command=="quit") quit=true;
            else if(command=="host")
            {
                std::cout << "TEST HOST {\"active_games\":" << gGHost->m_Games.size()
                          << ",\"lobby\":" << TestHostGameJSON(gGHost->m_CurrentGame) << ",\"games\":[";
                for(size_t i=0;i<gGHost->m_Games.size();++i)
                    std::cout << (i?",":"") << TestHostGameJSON(gGHost->m_Games[i]);
                std::cout << "]}" << std::endl;
            }
            else if(command=="autohost") gGHost->m_LastAutoHostTime-=31;
            else if(command=="rehosterror" && gGHost->m_CurrentGame) gGHost->m_CurrentGame->SetRefreshError(true);
            else if(command=="removehost" && gGHost->m_CurrentGame) { delete gGHost->m_CurrentGame; gGHost->m_CurrentGame=NULL; }
            else if(command=="create" && !gGHost->m_CurrentGame) { gGHost->m_CurrentGame=new NativeGame(gGHost); gGHost->m_CurrentGame->UpdateExternalPublication(); std::cout << "TEST KEY " << static_cast<NativeGame*>(gGHost->m_CurrentGame)->key() << std::endl; }
            else
            {
                NativeGame *game=dynamic_cast<NativeGame*>(gGHost->m_CurrentGame);
                if(!game && !gGHost->m_Games.empty()) game=dynamic_cast<NativeGame*>(gGHost->m_Games.back());
                if(game) { size_t sep=command.find(' '); game->test(command.substr(0,sep),sep==string::npos?"":command.substr(sep+1)); }
                else if(gGHost->m_CurrentGame && command=="slots") gGHost->m_CurrentGame->SendAllSlotInfo();
                else if(gGHost->m_CurrentGame && command=="ping") { CGameProtocol protocol(gGHost); gGHost->m_CurrentGame->SendAll(protocol.SEND_W3GS_PING_FROM_HOST()); }
            }
            std::cout << "TEST COMMAND " << command << std::endl;
        }
        if(!quit && gGHost->Update(10000)) break;
    }
    // Closing a CGame can enqueue SQLite player/stat callables. Exercise the
    // normal host event loop to drain them before deleting CGHost; its upstream
    // destructor intentionally leaves unfinished callables for process exit.
    // Delete the lobby directly so quit still tests pending-refresh destruction.
    gGHost->m_AutoHostMaximumGames=0;
    delete gGHost->m_CurrentGame; gGHost->m_CurrentGame=NULL;
    for(size_t i=0;i<gGHost->m_Games.size();++i) gGHost->m_Games[i]->StopPlayers("test completed");
    gGHost->m_ExitingNice=true;
    uint32_t draining=GetTicks();
    while(GetTicks()-draining<5000)
        if(gGHost->Update(10000)) break;
    bool clean=gGHost->m_Games.empty() && gGHost->m_Callables.empty();
    std::cout << "TEST DRAIN " << gGHost->m_Games.size() << ' ' << gGHost->m_Callables.size() << std::endl;
    delete gGHost; gGHost=NULL; return clean?0:3;
}
