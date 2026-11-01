// Test driver only: runs real CGHost/CBaseGame/W3GS code with synthetic metadata.
// No account, external socket, game engine, or replacement authentication state.
#include "ghost.h"
#include "config.h"
#include "game_base.h"
#include "gameprotocol.h"
#include "gameplayer.h"
#include "map.h"
#include "util.h"
#include <fcntl.h>
#include <unistd.h>
#include <iostream>
extern CGHost *gGHost;
int main( int argc, char **argv )
{
    if( argc != 2 ) return 2;
    CConfig cfg;
    cfg.Read( argv[1] );
    gGHost = new CGHost( &cfg );
    if( gGHost->m_Exiting ) { delete gGHost; gGHost = NULL; return 78; }
    fcntl( 0, F_SETFL, fcntl( 0, F_GETFL ) | O_NONBLOCK );
    std::cout << "TEST READY " << gGHost->m_BNETs.size( ) << std::endl;
    string pending;
    uint32_t start = GetTicks( );
    bool quit = false;
    while( !quit && GetTicks( ) - start < 60000 )
    {
        char bytes[512];
        ssize_t n = read( 0, bytes, sizeof bytes );
        if( n > 0 ) pending.append( bytes, n );
        size_t end;
        while( ( end = pending.find( '\n' ) ) != string::npos )
        {
            string command = pending.substr( 0, end );
            pending.erase( 0, end + 1 );
            if( command == "create" )
                gGHost->CreateGame( gGHost->m_Map, GAME_PUBLIC, false, "Synthetic lobby", "Owner", "Owner", "", false );
            else if( command == "remove" )
            { delete gGHost->m_CurrentGame; gGHost->m_CurrentGame = NULL; }
            else if( command == "start" && gGHost->m_CurrentGame )
                gGHost->m_CurrentGame->EventGameStarted( );
            else if( ( command == "private" || command == "public" ) && gGHost->m_CurrentGame )
            {
                CGamePlayer *owner = gGHost->m_CurrentGame->GetPlayerFromName( "Owner", false );
                if( owner ) gGHost->m_CurrentGame->EventPlayerBotCommand( owner, command == "private" ? "priv" : "pub", command == "private" ? "Private lobby" : "Public lobby" );
            }
            else if( command.substr( 0, 8 ) == "inspect " && gGHost->m_CurrentGame )
            {
                CGamePlayer *player = gGHost->m_CurrentGame->GetPlayerFromName( command.substr( 8 ), false );
                std::cout << "TEST PLAYER " << ( player ? 1 : 0 ) << " " << ( player && player->GetSpoofed( ) ? 1 : 0 ) << " " << ( player && !player->GetJoinedRealm( ).empty( ) ? 1 : 0 ) << std::endl;
            }
            else if( command == "quit" ) quit = true;
            std::cout << "TEST COMMAND " << command << std::endl;
        }
        if( !quit && gGHost->Update( 10000 ) ) break;
    }
    delete gGHost;
    gGHost = NULL;
    return 0;
}
